from pathlib import Path
import shlex
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "utility-apps/argocd/image-updater"
COMPONENTS = {
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
}
REGISTRY = "harbor.internal.api-api-api.com"
GIT_REPOSITORY = "https://github.com/anomaly51/general-1-argocd.git"
WEBHOOK = "playground-image-updater-webhook"
WEBHOOK_HOST = WEBHOOK + ".internal.api-api-api.com"
SERVICE = "playground-image-updater"


def run_helm(*arguments):
    command = ["helm", *map(str, arguments)]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Helm command failed ({error.returncode}): {shlex.join(command)}\n"
            f"stdout:\n{error.stdout or '<empty>'}\n"
            f"stderr:\n{error.stderr or '<empty>'}"
        ) from error
    return result.stdout


def ensure_chart_dependency(chart):
    archive = chart / "charts/argocd-image-updater-1.3.1.tgz"
    if archive.is_file():
        return archive
    # Fresh CI runners have no Helm repositories. Keep bootstrap independent of
    # the developer's repository list and avoid refreshing unrelated repositories.
    with tempfile.TemporaryDirectory(prefix="playground-image-updater-helm-") as directory:
        configuration = [
            "--repository-config", str(Path(directory) / "repositories.yaml"),
            "--repository-cache", str(Path(directory) / "repository"),
        ]
        run_helm("repo", "add", "playground-test-argo",
                 "https://argoproj.github.io/argo-helm", *configuration)
        run_helm("dependency", "build", chart, "--skip-refresh", *configuration)
    return archive


class HelmBootstrapTests(unittest.TestCase):
    def test_existing_dependency_needs_no_repository_access(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory)
            archive = chart / "charts/argocd-image-updater-1.3.1.tgz"
            archive.parent.mkdir()
            archive.touch()
            with patch(__name__ + ".run_helm") as helm:
                self.assertEqual(ensure_chart_dependency(chart), archive)
            helm.assert_not_called()

    def test_missing_dependency_bootstraps_an_isolated_repository(self):
        with tempfile.TemporaryDirectory() as directory:
            chart = Path(directory)
            with patch(__name__ + ".run_helm") as helm:
                archive = ensure_chart_dependency(chart)
            self.assertEqual(archive, chart / "charts/argocd-image-updater-1.3.1.tgz")
            self.assertEqual(helm.call_count, 2)
            add, build = [call.args for call in helm.call_args_list]
            self.assertEqual(add[:4], ("repo", "add", "playground-test-argo",
                                      "https://argoproj.github.io/argo-helm"))
            self.assertEqual(build[:4], ("dependency", "build", chart, "--skip-refresh"))
            self.assertEqual(add[4:], build[4:])
            self.assertEqual(add[4], "--repository-config")
            self.assertEqual(add[6], "--repository-cache")
            self.assertEqual(Path(add[5]).parent, Path(add[7]).parent)
            self.assertFalse(Path(add[5]).parent.exists(), "Temporary Helm configuration leaked")

    def test_failure_reports_command_and_captured_helm_diagnostics(self):
        failure = subprocess.CalledProcessError(
            1, ["helm", "dependency", "build"],
            output="Resolving chart dependencies", stderr="Error: no repository definition",
        )
        with patch.object(subprocess, "run", side_effect=failure):
            with self.assertRaises(RuntimeError) as result:
                run_helm("dependency", "build", "/tmp/example-chart")
        message = str(result.exception)
        self.assertIn("helm dependency build /tmp/example-chart", message)
        self.assertIn("Resolving chart dependencies", message)
        self.assertIn("Error: no repository definition", message)


class PlaygroundImageUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = ensure_chart_dependency(CHART)
        rendered = run_helm("template", "image-updater", CHART, "--namespace", "argocd")
        cls.documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
        cls.config = yaml.safe_load((CHART / "values.yaml").read_text())
        cls.updaters = [doc for doc in cls.documents if doc["kind"] == "ImageUpdater"]
        cls.references = [ref for updater in cls.updaters for ref in updater["spec"]["applicationRefs"]]
        cls.deployment = next(doc for doc in cls.documents if doc["kind"] == "Deployment")
        cls.pod = cls.deployment["spec"]["template"]["spec"]
        cls.pod_labels = cls.deployment["spec"]["template"]["metadata"]["labels"]

    def document(self, kind, name):
        return next(doc for doc in self.documents
                    if doc["kind"] == kind and doc["metadata"]["name"] == name)

    def test_webhook_needs_no_custom_cluster_dns_override(self):
        self.assertFalse((ROOT / "cluster/coredns-custom.yaml").exists())
        kustomization = yaml.safe_load((ROOT / "cluster/kustomization.yaml").read_text())
        self.assertNotIn("coredns-custom.yaml", kustomization["resources"])

    def test_upstream_chart_and_controller_are_exactly_pinned(self):
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        lock = yaml.safe_load((CHART / "Chart.lock").read_text())
        expected = [{"name": "argocd-image-updater", "version": "1.3.1",
                     "repository": "https://argoproj.github.io/argo-helm"}]
        self.assertEqual(chart["dependencies"], expected)
        self.assertEqual(lock["dependencies"], expected)
        self.assertRegex(lock["digest"], r"^sha256:[0-9a-f]{64}$")
        with tarfile.open(self.archive) as archive:
            upstream = yaml.safe_load(archive.extractfile("argocd-image-updater/Chart.yaml"))
        self.assertEqual(upstream["version"], "1.3.1")
        self.assertEqual(upstream["appVersion"], "v1.3.0")
        deployment = self.document("Deployment", "playground-image-updater-controller")
        image = deployment["spec"]["template"]["spec"]["containers"][0]["image"]
        self.assertEqual(image, "quay.io/argoprojlabs/argocd-image-updater:v1.3.0")

    def test_only_exact_nonproduction_playground_applications_are_selected(self):
        environments = self.config["environments"]
        self.assertLessEqual(set(environments), {"dev", "staging"})
        self.assertEqual(len(environments), len(set(environments)))
        self.assertEqual(len(self.references), 8 * len(environments))
        self.assertEqual({ref["namePattern"] for ref in self.references},
                         {f"playground-{name}-{environment}" for name in COMPONENTS
                          for environment in environments})
        for ref in self.references:
            self.assertFalse(ref["useAnnotations"])
            self.assertNotIn("labelSelectors", ref)
            name, environment = ref["namePattern"].removeprefix("playground-").rsplit("-", 1)
            self.assertEqual(ref["images"], [{
                "alias": name,
                "imageName": f"{REGISTRY}/playground/{name}:{environment}",
                "commonUpdateSettings": {
                    "updateStrategy": "digest",
                    "platforms": ["linux/amd64"],
                    "pullSecret": "pullsecret:argocd/playground-image-updater-registry",
                },
                "manifestTargets": {"helm": {"name": "image.repository", "tag": "image.tag"}},
            }])

    def test_every_application_writes_only_its_authoritative_nonproduction_values(self):
        for updater in self.updaters:
            self.assertNotIn("writeBackConfig", updater["spec"])
        for ref in self.references:
            name, environment = ref["namePattern"].removeprefix("playground-").rsplit("-", 1)
            self.assertEqual(ref["writeBackConfig"], {
                "method": "git:secret:playground-image-updater-git",
                "gitConfig": {
                    "repository": GIT_REPOSITORY,
                    "branch": "main",
                    "writeBackTarget": f"helmvalues:/apps/playground-{name}/values/{environment}.yaml",
                },
            })
            values_path = ROOT / f"apps/playground-{name}/values/{environment}.yaml"
            values = yaml.safe_load(values_path.read_text())
            self.assertEqual(values["image"]["repository"], f"{REGISTRY}/playground/{name}")
            self.assertIsInstance(values["image"]["tag"], str)
            self.assertEqual(values["_release"]["namespace"], f"playground-{environment}")
            self.assertEqual(values["_release"]["repository"], f"{REGISTRY}/helm-charts")
            self.assertEqual(values["_release"]["chart"], "app")
            self.assertRegex(values["_release"]["revision"], r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
        self.assertNotIn(".argocd-source", yaml.safe_dump(self.updaters))

    def test_production_is_manual_and_never_an_updater_target(self):
        for name in COMPONENTS:
            values = yaml.safe_load((ROOT / f"apps/playground-{name}/values/prod.yaml").read_text())
            self.assertEqual(values["_release"]["policy"], "promote")
            self.assertEqual(values["_release"]["namespace"], "playground-prod")
            self.assertRegex(values["image"]["tag"], r"^[^@]+@sha256:[0-9a-f]{64}$")
        self.assertNotIn("/values/prod.yaml", yaml.safe_dump(self.updaters))
        self.assertFalse(any(ref["namePattern"].startswith("apps-") for ref in self.references))
        with self.assertRaisesRegex(RuntimeError, "Image Updater may only manage dev and staging"):
            run_helm("template", "image-updater", CHART, "--namespace", "argocd",
                     "--set", "environments[0]=prod")

    def test_appset_keeps_main_values_separate_from_pinned_chart(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/apps.yaml").read_text())
        self.assertEqual(appset["spec"]["generators"][0]["git"]["revision"], "main")
        source = appset["spec"]["template"]["spec"]["source"]
        self.assertEqual(source["targetRevision"], "{{ ._release.revision }}")
        self.assertIn('dig "repository"', source["repoURL"])
        self.assertIn('if not (hasKey ._release "repository")', source["path"])
        self.assertIn('dig "chart" (index .path.segments 1) ._release', source["chart"])
        self.assertIn('omit . "_release" "path"', source["helm"]["values"])
        self.assertIn('"prod-only"', appset["spec"]["templatePatch"])

    def test_namespaced_controller_has_bounded_resources_and_no_public_listener(self):
        forbidden = {"ClusterRole", "ClusterRoleBinding", "Ingress", "HTTPRoute", "Gateway"}
        self.assertFalse(forbidden & {doc["kind"] for doc in self.documents})
        for doc in self.documents:
            if doc["kind"] != "CustomResourceDefinition":
                self.assertEqual(doc["metadata"].get("namespace"), "argocd")
        pod = self.document("Deployment", "playground-image-updater-controller")["spec"]["template"]["spec"]
        self.assertEqual(pod["serviceAccountName"], "playground-image-updater")
        self.assertEqual(pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"], {
            "nodeSelectorTerms": [{"matchExpressions": [{
                "key": "node-role.kubernetes.io/control-plane", "operator": "DoesNotExist",
            }]}],
        })
        resources = pod["containers"][0]["resources"]
        self.assertEqual(resources["requests"]["memory"], "128Mi")
        self.assertEqual(resources["limits"]["memory"], "256Mi")
        config = self.document("ConfigMap", "playground-image-updater-config")["data"]
        self.assertEqual(config["argocd.namespace"], "argocd")
        self.assertEqual(config["watch.namespaces"], "argocd")
        self.assertEqual(config["interval"], "2m")
        self.assertEqual(config["webhook.enable"], "true")
        self.assertEqual(config["disable-tls"], "true")
        # In v1.3.0 kube.events maps to DisableKubeEvents, not EnableKubeEvents.
        self.assertIn("--disable-kube-events", pod["containers"][0]["args"])
        registry = yaml.safe_load(config["registries.conf"])["registries"][0]
        self.assertEqual(registry["credentials"], "pullsecret:argocd/playground-image-updater-registry")
        self.assertFalse(registry["insecure"])

    def test_builtin_private_service_selects_the_rendered_webhook_port_only(self):
        services = [doc for doc in self.documents if doc["kind"] == "Service"]
        self.assertEqual([service["metadata"]["name"] for service in services], [SERVICE])
        service = services[0]["spec"]
        self.assertEqual(service["type"], "ClusterIP")
        self.assertNotIn("clusterIP", service, "Service IP must be allocated by Kubernetes")
        self.assertNotIn("externalIPs", service)
        self.assertNotIn("loadBalancerIP", service)
        self.assertTrue(service["selector"])
        self.assertLessEqual(service["selector"].items(), self.pod_labels.items())
        self.assertEqual(service["ports"], [{
            "name": "server-port", "port": 8080, "targetPort": "webhook", "protocol": "TCP",
        }])
        ports = {port["name"]: port["containerPort"] for port in self.pod["containers"][0]["ports"]}
        self.assertEqual(ports[service["ports"][0]["targetPort"]], 8082)
        # Disabling the upstream Service must remove every Service, proving that
        # the wrapper has not retained a second, bespoke webhook Service.
        rendered = run_helm("template", "image-updater", CHART, "--namespace", "argocd",
                            "--set", "argocd-image-updater.service.enabled=false")
        self.assertFalse(any(doc and doc["kind"] == "Service" for doc in yaml.safe_load_all(rendered)))

    def test_webhook_has_no_proxy_sidecar_or_custom_tls_resources(self):
        self.assertEqual(len(self.pod["containers"]), 1)
        self.assertFalse(self.pod.get("initContainers"))
        self.assertFalse((CHART / "files/nginx.conf").exists())
        annotations = self.deployment["spec"]["template"]["metadata"].get("annotations", {})
        self.assertNotIn("checksum/webhook-proxy", annotations)
        self.assertFalse(any(doc["kind"] == "Certificate" for doc in self.documents))
        self.assertFalse(any(doc["kind"] == "ConfigMap" and doc["metadata"]["name"] == WEBHOOK
                             for doc in self.documents))
        # The upstream chart retains its optional default TLS volume. No custom
        # proxy/certificate/tmp volume or required certificate may be added.
        self.assertFalse({"webhook-nginx", "webhook-tls", "webhook-tmp"}
                         & {volume["name"] for volume in self.pod.get("volumes", [])})
        for volume in self.pod.get("volumes", []):
            if volume["name"] == "argocd-image-updater-tls":
                self.assertTrue(volume["secret"]["optional"])

    def test_network_policy_allows_only_harbor_senders_on_webhook_port(self):
        policies = [doc for doc in self.documents if doc["kind"] == "NetworkPolicy"]
        self.assertEqual(len(policies), 1)
        policy = policies[0]["spec"]
        selector = policy["podSelector"]["matchLabels"]
        self.assertTrue(selector)
        self.assertLessEqual(selector.items(), self.pod_labels.items())
        self.assertEqual(policy["policyTypes"], ["Ingress"])
        self.assertEqual(len(policy["ingress"]), 1)
        ingress = policy["ingress"][0]
        self.assertEqual(ingress["ports"], [{"protocol": "TCP", "port": 8082}])
        # Both selectors must be in one peer: harbor namespace AND approved pods.
        self.assertEqual(ingress["from"], [{
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "harbor"}},
            "podSelector": {
                "matchLabels": {"app.kubernetes.io/name": "harbor"},
                "matchExpressions": [{
                    "key": "app.kubernetes.io/component", "operator": "In",
                    "values": ["core", "jobservice"],
                }],
            },
        }])
        self.assertNotIn("egress", policy)

    def test_webhook_uses_service_dns_without_a_custom_private_record(self):
        rendered = run_helm("template", "internal-dns", ROOT / "utility-apps/networking/internal-dns",
                            "--namespace", "networking")
        documents = [doc for doc in yaml.safe_load_all(rendered) if doc]
        dns = next(doc for doc in documents if doc["kind"] == "ConfigMap")
        zone = dns["data"]["internal.db"]
        self.assertIn("$ORIGIN internal.api-api-api.com.", zone)
        self.assertNotIn(WEBHOOK, zone)
        self.assertIn("*  IN A 192.168.1.3", zone)
        self.assertIn("ns IN A 192.168.1.2", zone)
        config = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertNotIn("hostname", config.get("webhook", {}))
        self.assertNotIn("clusterIP", config.get("webhook", {}))
        self.assertNotIn(WEBHOOK_HOST, yaml.safe_dump(self.documents))

    def test_webhook_secret_is_vault_managed_injected_and_restarts_on_rotation(self):
        secret = self.document("VaultStaticSecret", WEBHOOK)["spec"]
        self.assertEqual(secret["vaultAuthRef"], "playground-image-updater")
        self.assertEqual((secret["mount"], secret["type"]), ("kv", "kv-v2"))
        self.assertEqual(secret["path"], "ci/playground-image-updater-webhook")
        self.assertEqual(secret["refreshAfter"], "1m")
        destination = secret["destination"]
        self.assertTrue(destination["create"])
        self.assertEqual(destination["name"], "argocd-image-updater-secret")
        self.assertEqual(destination["transformation"], {
            "excludeRaw": True, "excludes": [".*"],
            "templates": {"webhook.harbor-secret": {"text": '{{- get .Secrets "secret" -}}'}},
        })
        environment = {item["name"]: item for item in self.pod["containers"][0]["env"]}
        reference = environment["HARBOR_WEBHOOK_SECRET"]["valueFrom"]["secretKeyRef"]
        self.assertEqual(reference["name"], destination["name"])
        self.assertEqual(reference["key"], "webhook.harbor-secret")
        self.assertNotIn("value", environment["HARBOR_WEBHOOK_SECRET"])
        self.assertEqual(secret["rolloutRestartTargets"], [{
            "kind": "Deployment", "name": self.deployment["metadata"]["name"],
        }])
        self.assertNotIn("--webhook-require-secret=false", self.pod["containers"][0]["args"])
        self.assertNotIn("WEBHOOK_REQUIRE_SECRET", environment)

    def test_git_only_rbac_cannot_modify_applications_or_secrets(self):
        application_rules = []
        for doc in self.documents:
            if doc["kind"] != "Role":
                continue
            for rule in doc["rules"]:
                resources = set(rule["resources"])
                self.assertNotIn("*", resources)
                if "applications" in resources:
                    application_rules.append(rule)
                if resources & {"applications", "secrets"}:
                    self.assertLessEqual(set(rule["verbs"]), {"get", "list", "watch"})
        self.assertTrue(application_rules)
        role = self.document("Role", "playground-image-updater-git-only")
        self.assertTrue(any("imageupdaters/status" in rule["resources"] for rule in role["rules"]))
        self.assertTrue(any("imageupdaters/finalizers" in rule["resources"] for rule in role["rules"]))
        leader = self.document("Role", "playground-image-updater-leader-election-role")
        self.assertTrue(any("leases" in rule["resources"] for rule in leader["rules"]))

    def test_vault_auth_uses_its_dedicated_service_account(self):
        auth = self.document("VaultAuth", "playground-image-updater")["spec"]
        self.assertEqual(auth["method"], "kubernetes")
        self.assertEqual(auth["mount"], "kubernetes")
        self.assertEqual(auth["kubernetes"], {
            "role": "playground-image-updater", "serviceAccount": "playground-image-updater",
            "audiences": ["vault"], "tokenExpirationSeconds": 600,
        })

    def test_vault_secrets_use_only_explicit_mapped_fields(self):
        registry = self.document("VaultStaticSecret", "playground-image-updater-registry")["spec"]
        git = self.document("VaultStaticSecret", "playground-image-updater-git")["spec"]
        for secret in [registry, git]:
            self.assertEqual(secret["vaultAuthRef"], "playground-image-updater")
            self.assertEqual(secret["mount"], "kv")
            self.assertEqual(secret["type"], "kv-v2")
            transformation = secret["destination"]["transformation"]
            self.assertTrue(transformation["excludeRaw"])
            self.assertEqual(transformation["excludes"], [".*"])
        self.assertEqual(registry["path"], "apps/playground-prod/registry")
        self.assertEqual(registry["destination"]["type"], "kubernetes.io/dockerconfigjson")
        docker = registry["destination"]["transformation"]["templates"]
        self.assertEqual(set(docker), {".dockerconfigjson"})
        self.assertIn('"auth" (printf "%s:%s" $username $password | b64enc)', docker[".dockerconfigjson"]["text"])
        self.assertIn(REGISTRY, docker[".dockerconfigjson"]["text"])
        self.assertEqual(git["path"], "ci/github-app")
        self.assertEqual(git["destination"]["type"], "Opaque")
        mappings = git["destination"]["transformation"]["templates"]
        expected = {"githubAppID": "app_id", "githubAppInstallationID": "installation_id",
                    "githubAppPrivateKey": "private_key"}
        self.assertEqual(set(mappings), set(expected))
        for destination, source in expected.items():
            self.assertEqual(mappings[destination]["text"], '{{- get .Secrets "' + source + '" -}}')

    def test_existing_argocd_resources_are_not_claimed_or_replaced(self):
        protected = {"argocd-secret", "argocd-cm", "argocd-rbac-cm", "argocd-applicationset-controller"}
        self.assertFalse(protected & {doc["metadata"]["name"] for doc in self.documents})
        self.assertFalse({"Application", "ApplicationSet", "Secret", "Job"}
                         & {doc["kind"] for doc in self.documents})
        destinations = {doc["spec"]["destination"]["name"] for doc in self.documents
                        if doc["kind"] == "VaultStaticSecret"}
        self.assertEqual(destinations, {"playground-image-updater-registry", "playground-image-updater-git",
                                        "argocd-image-updater-secret"})

    def test_custom_resource_fields_match_the_bundled_upstream_crd(self):
        crd = self.document("CustomResourceDefinition", "imageupdaters.argocd-image-updater.argoproj.io")
        version = next(version for version in crd["spec"]["versions"] if version["name"] == "v1alpha1")

        def check(value, schema, path):
            if isinstance(value, dict):
                self.assertLessEqual(set(schema.get("required", [])), set(value), path)
                properties = schema.get("properties")
                if properties:
                    self.assertLessEqual(set(value), set(properties), path)
                    for key, child in value.items():
                        check(child, properties[key], path + "." + key)
            elif isinstance(value, list):
                for index, child in enumerate(value):
                    check(child, schema["items"], path + f"[{index}]")

        for updater in self.updaters:
            check(updater, version["schema"]["openAPIV3Schema"], "ImageUpdater")


if __name__ == "__main__":
    unittest.main()
