"""Offline contracts for restored ARC, MinIO, and the isolated Argo runtime.

Build pinned Helm dependencies before running this suite. Restore behavior tests
use disposable local fixtures; no cluster, Vault, or retained NFS data is touched.
"""

from functools import lru_cache
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
import tomllib
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHARTS = {
    "arc-controller": ("arc-systems", "arc-controller"),
    "arc-secrets": ("arc-runners", "secrets"),
    "arc-runner-set": ("arc-runners", "arc-runner-set"),
    "juggluco-runner-set": ("arc-runners", "juggluco-runner-set"),
    "arc-buildkit": ("arc-runners", "arc-buildkit"),
    "minio": ("minio-system", "minio"),
    "argo": ("argo-workflows", "engine"),
}
CLIENT_LABEL = "ci.api-api-api.com/buildkit-client"


@lru_cache(maxsize=None)
def documents(chart):
    namespace, release = CHARTS[chart]
    rendered = subprocess.check_output([
        "helm", "template", release, str(ROOT / "utility-apps" / namespace / release),
        "--namespace", namespace, "--kube-version", "1.33.4",
    ], text=True)
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def resource(chart, kind, name):
    return next(doc for doc in documents(chart)
                if doc["kind"] == kind and doc["metadata"]["name"] == name)


def runner_set(chart):
    return next(doc for doc in documents(chart) if doc["kind"] == "AutoscalingRunnerSet")


class ArcMigrationTests(unittest.TestCase):
    def test_all_charts_are_auto_discovered_with_expected_releases_and_namespaces(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/utility-apps.yaml").read_text())
        generator = appset["spec"]["generators"][0]["git"]
        self.assertIn({"path": "utility-apps/*/*"}, generator["directories"])
        self.assertEqual(appset["spec"]["template"]["spec"]["source"]["helm"]["releaseName"],
                         "{{ .path.basenameNormalized }}")
        self.assertEqual(appset["spec"]["template"]["spec"]["destination"]["namespace"],
                         "{{ index .path.segments 1 }}")
        for key, (namespace, release) in CHARTS.items():
            with self.subTest(chart=key):
                self.assertTrue((ROOT / "utility-apps" / namespace / release / "Chart.yaml").is_file())
                for doc in documents(key):
                    self.assertIn(doc["metadata"].get("namespace"), (None, namespace))

    def test_chart_dependencies_and_runtime_images_are_pinned(self):
        for name in ("arc-controller", "arc-runner-set", "juggluco-runner-set"):
            namespace, release = CHARTS[name]
            chart_dir = ROOT / "utility-apps" / namespace / release
            chart = yaml.safe_load((chart_dir / "Chart.yaml").read_text())
            lock = yaml.safe_load((chart_dir / "Chart.lock").read_text())
            self.assertEqual(chart["dependencies"][0]["version"], "0.14.2")
            self.assertEqual(lock["dependencies"][0]["version"], "0.14.2")
        for name in ("arc-runner-set", "juggluco-runner-set"):
            spec = runner_set(name)["spec"]["template"]["spec"]
            for container in spec["containers"] + spec["initContainers"]:
                self.assertRegex(container["image"], r"@sha256:[0-9a-f]{64}$")
                self.assertNotIn(":latest", container["image"])

    def test_runner_identity_concurrency_and_juggluco_group_are_preserved(self):
        for name, identity, minimum, maximum in (
            ("arc-runner-set", "arc-runner-set", 1, 2),
            ("juggluco-runner-set", "juggluco-deploy", 0, 1),
        ):
            with self.subTest(chart=name):
                spec = runner_set(name)["spec"]
                self.assertEqual(spec["runnerScaleSetName"], identity)
                self.assertEqual(spec["minRunners"], minimum)
                self.assertEqual(spec["maxRunners"], maximum)
                self.assertEqual(spec["githubConfigSecret"], "arc-github-token")
                self.assertEqual(spec["githubConfigUrl"], "https://github.com/anomaly51")
                if name == "juggluco-runner-set":
                    self.assertEqual(spec["runnerGroup"], "Juggluco Deploy")

    def test_controller_does_not_wait_on_warm_runners_to_replace_listener(self):
        deployment = resource("arc-controller", "Deployment", "arc-controller-gha-rs-controller")
        containers = deployment["spec"]["template"]["spec"]["containers"]
        manager = next(container for container in containers if container["name"] == "manager")
        self.assertIn("--update-strategy=immediate", manager["args"])
        self.assertNotIn("--update-strategy=eventual", manager["args"])

    def test_runner_work_uses_expanded_worker_one_and_one_gib_memory_ceiling(self):
        for name in ("arc-runner-set", "juggluco-runner-set"):
            with self.subTest(chart=name):
                template = runner_set(name)["spec"]["template"]
                self.assertEqual(template["metadata"]["labels"][CLIENT_LABEL], "true")
                spec = template["spec"]
                self.assertEqual(spec["nodeSelector"], {"kubernetes.io/hostname": "general-1-worker-1"})
                self.assertIs(spec["automountServiceAccountToken"], False)
                self.assertNotIn("hostAliases", spec)
                runner = next(c for c in spec["containers"] if c["name"] == "runner")
                dind = next(c for c in spec["initContainers"] if c["name"] == "dind")
                self.assertEqual(dind["restartPolicy"], "Always")
                self.assertTrue(dind["securityContext"]["privileged"])
                for container in (runner, dind):
                    self.assertEqual(container["resources"]["limits"]["memory"], "512Mi")
                    for bound in ("requests", "limits"):
                        self.assertLessEqual({"cpu", "memory", "ephemeral-storage"},
                                             set(container["resources"][bound]))
                self.assertNotIn("envFrom", runner)
                self.assertTrue(all(v["emptyDir"].get("sizeLimit") for v in spec["volumes"]))
                self.assertFalse(any(doc["kind"] == "CronJob" for doc in documents(name)))

    def test_vault_registration_credential_is_scoped_and_not_embedded(self):
        auth = resource("arc-secrets", "VaultAuth", "arc-runners")["spec"]
        self.assertEqual(auth["method"], "kubernetes")
        self.assertEqual(auth["mount"], "kubernetes")
        self.assertEqual(auth["kubernetes"], {
            "role": "arc-runners", "serviceAccount": "vault-secrets",
            "audiences": ["vault"], "tokenExpirationSeconds": 600,
        })
        secret = resource("arc-secrets", "VaultStaticSecret", "arc-github-token")["spec"]
        self.assertEqual((secret["mount"], secret["type"], secret["path"]),
                         ("kv", "kv-v2", "ci/arc-github"))
        transform = secret["destination"]["transformation"]
        self.assertTrue(transform["excludeRaw"])
        self.assertEqual(transform["excludes"], [".*"])
        self.assertEqual(transform["templates"], {"github_token": {"text": '{{ get .Secrets "token" }}'}})
        self.assertFalse(any(doc["kind"] == "Secret" for doc in documents("arc-secrets")))
        for name in ("arc-runner-set", "juggluco-runner-set"):
            bindings = [doc for doc in documents(name) if doc["kind"] == "RoleBinding"]
            self.assertTrue(any(subject["name"] == "arc-controller-gha-rs-controller"
                                and subject["namespace"] == "arc-systems"
                                for binding in bindings for subject in binding["subjects"]))

    def test_remote_builder_is_node_bound_bounded_and_not_public(self):
        pod = resource("arc-buildkit", "Deployment", "arc-buildkit")["spec"]["template"]["spec"]
        self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "general-1-worker-3"})
        self.assertFalse(pod["automountServiceAccountToken"])
        self.assertNotIn("hostAliases", pod)
        container = pod["containers"][0]
        self.assertEqual(container["image"], "moby/buildkit:v0.32.2")
        self.assertEqual(container["resources"]["limits"]["memory"], "2Gi")
        self.assertEqual(container["resources"]["limits"]["cpu"], "2")
        self.assertIn("tcp://0.0.0.0:1234", container["args"])
        service = resource("arc-buildkit", "Service", "arc-buildkit")["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertEqual(service["ports"][0]["port"], 1234)
        pvc = resource("arc-buildkit", "PersistentVolumeClaim", "arc-buildkit-cache")["spec"]
        self.assertEqual(pvc["storageClassName"], "local-path")
        self.assertEqual(pvc["resources"]["requests"]["storage"], "30Gi")
        config = tomllib.loads(resource("arc-buildkit", "ConfigMap", "arc-buildkit")["data"]["buildkitd.toml"])
        worker = config["worker"]["oci"]
        self.assertEqual(worker["max-parallelism"], 2)
        self.assertTrue(worker["gc"])
        self.assertEqual(worker["maxUsedSpace"], "20GB")
        self.assertEqual(worker["minFreeSpace"], "10GB")
        self.assertTrue(worker["gcpolicy"][0]["all"])
        self.assertFalse(config["worker"]["containerd"]["enabled"])
        policy = resource("arc-buildkit", "NetworkPolicy", "arc-buildkit")["spec"]
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertEqual(policy["ingress"], [{
            "from": [{"podSelector": {"matchLabels": {CLIENT_LABEL: "true"}}}],
            "ports": [{"protocol": "TCP", "port": 1234}],
        }])


class MinioRestoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pod = resource("minio", "StatefulSet", "minio")["spec"]["template"]["spec"]
        cls.init = cls.pod["initContainers"][0]
        cls.script = cls.init["args"][0]

    def test_source_is_read_only_and_credentials_are_vault_only(self):
        volumes = {v["name"]: v for v in self.pod["volumes"]}
        self.assertTrue(volumes["retained-source"]["nfs"]["readOnly"])
        self.assertEqual(volumes["retained-source"]["nfs"]["server"], "192.168.1.9")
        self.assertTrue(volumes["retained-source"]["nfs"]["path"].startswith("/export/test/minio-system-data-minio-0-pvc-"))
        mounts = {m["name"]: m for m in self.init["volumeMounts"]}
        self.assertTrue(mounts["retained-source"]["readOnly"])
        self.assertFalse(self.pod["automountServiceAccountToken"])
        self.assertEqual(self.pod["securityContext"]["runAsUser"], 1000)
        self.assertTrue(self.init["securityContext"]["readOnlyRootFilesystem"])
        secret = resource("minio", "VaultStaticSecret", "minio-root")["spec"]
        self.assertEqual(secret["path"], "platform/minio")
        self.assertEqual(set(secret["destination"]["transformation"]["templates"]),
                         {"root_user", "root_password"})
        forbidden = {"Secret", "Ingress", "HTTPRoute", "Gateway"}
        self.assertFalse(forbidden & {d["kind"] for d in documents("minio")})
        pvc = resource("minio", "PersistentVolumeClaim", "data-minio-0")
        self.assertEqual(pvc["metadata"]["annotations"]["argocd.argoproj.io/sync-options"],
                         "Delete=false,Prune=false")

    def fixture(self):
        temporary = tempfile.TemporaryDirectory(prefix="minio-restore-contract-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source, data, scratch = (root / name for name in ("source", "data", "scratch"))
        for directory in (source, data, scratch):
            directory.mkdir()
        (source / ".minio.sys").mkdir()
        (source / ".minio.sys/format.json").write_text('{"fixture": true}\n')
        (source / "terraform-state").mkdir()
        (source / "terraform-state/state.bin").write_bytes(b"retained fixture bytes\x00")
        # Replace only fixed mount paths; the production command remains intact.
        mounts = {"source": source, "data": data, "tmp": scratch}
        script = re.sub(r"/(source|data|tmp)(?=/|[\s)])",
                        lambda match: shlex.quote(str(mounts[match.group(1)])), self.script)
        return root, source, data, script

    def run_restore(self, script, env=None):
        return subprocess.run(["/bin/sh", "-ec", script], text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)

    @unittest.skipUnless(shutil.which("sha256sum"), "sha256sum required for restore behavior tests")
    def test_empty_destination_copies_verifies_and_preserves_source(self):
        _, source, data, script = self.fixture()
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
        result = self.run_restore(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((data / ".utility-restore-complete").is_file())
        for path, content in before.items():
            self.assertEqual((data / path).read_bytes(), content)
            self.assertEqual((source / path).read_bytes(), content)

    def test_nonempty_destination_is_never_overwritten(self):
        _, _, data, script = self.fixture()
        (data / "existing").write_text("keep me")
        result = self.run_restore(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to overwrite", result.stderr)
        self.assertEqual((data / "existing").read_text(), "keep me")
        self.assertFalse((data / ".utility-restore-complete").exists())

    def test_completion_marker_skips_copy_without_reverting_new_data(self):
        _, _, data, script = self.fixture()
        (data / ".utility-restore-complete").touch()
        (data / "new-data").write_text("written after restore")
        result = self.run_restore(script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((data / "new-data").read_text(), "written after restore")
        self.assertFalse((data / ".minio.sys").exists())

    def test_missing_source_identity_refuses_restore(self):
        _, source, data, script = self.fixture()
        (source / ".minio.sys/format.json").unlink()
        result = self.run_restore(script)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(data.iterdir()), [])

    @unittest.skipUnless(shutil.which("sha256sum"), "sha256sum required for restore behavior tests")
    def test_corrupted_copy_fails_verification_without_completion_marker(self):
        root, _, data, script = self.fixture()
        shim = root / "bin"
        shim.mkdir()
        executable = shim / "cp"
        executable.write_text(
            "#!/bin/sh\nset -e\n"
            f"{shlex.quote(shutil.which('cp'))} \"$@\"\n"
            f"printf corrupted > {shlex.quote(str(data / 'terraform-state/state.bin'))}\n"
        )
        executable.chmod(0o755)
        result = self.run_restore(script, dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"]))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((data / ".utility-restore-complete").exists())

    @unittest.skipUnless(shutil.which("sha256sum"), "sha256sum required for restore behavior tests")
    def test_failed_checksum_generation_never_marks_restore_complete(self):
        root, _, data, script = self.fixture()
        shim = root / "bin"
        shim.mkdir()
        executable = shim / "sha256sum"
        executable.write_text(
            "#!/bin/sh\n"
            "for argument in \"$@\"; do\n"
            "  case \"$argument\" in */format.json) exit 1 ;; esac\n"
            "done\n"
            f"exec {shlex.quote(shutil.which('sha256sum'))} \"$@\"\n"
        )
        executable.chmod(0o755)
        result = self.run_restore(script, dict(os.environ, PATH=str(shim) + os.pathsep + os.environ["PATH"]))
        self.assertNotEqual(result.returncode, 0, "Partial source checksum failures must fail closed")
        self.assertFalse((data / ".utility-restore-complete").exists())


class ArgoIsolationTests(unittest.TestCase):
    def test_runtime_requires_client_auth_and_does_not_restore_automation(self):
        docs = documents("argo")
        forbidden = {"Workflow", "WorkflowTemplate", "ClusterWorkflowTemplate", "CronWorkflow",
                     "Job", "CronJob", "Secret", "ExternalSecret", "VaultStaticSecret",
                     "Ingress", "IngressRoute", "HTTPRoute", "Gateway", "ClusterRole", "ClusterRoleBinding"}
        self.assertFalse(forbidden & {doc["kind"] for doc in docs})
        server = resource("argo", "Deployment", "argo-server")["spec"]["template"]["spec"]["containers"][0]
        self.assertIn("--auth-mode=client", server["args"])
        self.assertIn("--secure=true", server["args"])
        self.assertNotIn("--auth-mode=server", server["args"])
        self.assertIn("--namespaced", server["args"])
        for service in (doc for doc in docs if doc["kind"] == "Service"):
            self.assertEqual(service["spec"]["type"], "ClusterIP")
        endpoint = resource("argo", "ConfigMap", "utility-migration-endpoints")["data"]
        self.assertEqual(endpoint["minioEndpoint"], "minio.minio-system.svc.cluster.local:9000")
        self.assertEqual(endpoint["terraformWorkflowsEnabled"], "false")
        config = yaml.safe_load(resource("argo", "ConfigMap", "argo-workflow-controller-configmap")["data"]["config"])
        self.assertNotIn("artifactRepository", config)
        self.assertNotIn("persistence", config)
        self.assertEqual(config["parallelism"], 2)
        self.assertEqual(config["namespaceParallelism"], 2)


if __name__ == "__main__":
    unittest.main()
