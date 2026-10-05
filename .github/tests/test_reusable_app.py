"""Offline contracts for the thin playground wrappers and flat automation values.

The pinned app dependency is vendored in each wrapper, so a shallow checkout
must render without registry access or a sibling platform chart repository.
"""

from functools import lru_cache
import hashlib
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import promote_playground


COMPONENTS = (
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
)
ENVIRONMENTS = ("dev", "staging", "prod")
CHART_VERSION = "0.6.0"
REGISTRY = "oci://harbor.internal.api-api-api.com/helm-charts"


def profile(component, environment):
    return yaml.safe_load((ROOT / f"apps/playground-{component}/values/{environment}.yaml").read_text())


@lru_cache(maxsize=None)
def render(component, environment, override=False, ephemeral=False):
    values = profile(component, environment)
    namespace = values.pop("_release")["namespace"]
    if override:
        values["image"] = {"repository": "example.invalid/contract-image", "tag": "pinned"}
        values.setdefault("env", {})["REUSABLE_CHART_CONTRACT"] = "root-values"
        values["nodeSelector"] = {"kubernetes.io/hostname": "contract-worker"}
    if ephemeral:
        values["ephemeral"] = True
        namespace = "playground-preview-contract-0123456789"
    release = component if ephemeral else "playground-" + component
    result = subprocess.run(
        ["helm", "template", release, str(ROOT / f"apps/playground-{component}"),
         "--namespace", namespace, "--values", "-"],
        input=yaml.safe_dump(values), text=True, capture_output=True,
    )
    if result.returncode:
        raise AssertionError(f"{component}/{environment}: Helm failed:\n{result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


class ReusableAppWrapperTests(unittest.TestCase):
    def test_all_wrappers_have_one_identical_pinned_offline_dependency(self):
        hashes = set()
        for component in COMPONENTS:
            with self.subTest(component=component):
                chart_dir = ROOT / f"apps/playground-{component}"
                chart = yaml.safe_load((chart_dir / "Chart.yaml").read_text())
                lock = yaml.safe_load((chart_dir / "Chart.lock").read_text())
                dependency = {"name": "app", "version": CHART_VERSION, "repository": REGISTRY}
                self.assertEqual(chart["dependencies"], [dependency])
                self.assertEqual(lock["dependencies"], [dependency])
                self.assertRegex(lock["digest"], r"^sha256:[0-9a-f]{64}$")
                archive = chart_dir / f"charts/app-{CHART_VERSION}.tgz"
                hashes.add(hashlib.sha256(archive.read_bytes()).hexdigest())
                with tarfile.open(archive) as packaged:
                    metadata = yaml.safe_load(packaged.extractfile("app/Chart.yaml"))
                    self.assertEqual((metadata["name"], metadata["version"]), ("app", CHART_VERSION))
                self.assertFalse((chart_dir / "values.yaml").exists())
        self.assertEqual(len(hashes), 1, "All wrappers must use the same published app package")

    def test_wrappers_only_delegate_workload_rendering(self):
        for component in COMPONENTS:
            with self.subTest(component=component):
                files = [path for path in (ROOT / f"apps/playground-{component}/templates").rglob("*")
                         if path.is_file()]
                self.assertEqual(len(files), 1)
                source = files[0].read_text().strip()
                self.assertEqual(len(source.splitlines()), 1)
                self.assertEqual(len(re.findall(r'include\s+"app\.workloads"', source)), 1)
                self.assertIn(".Subcharts.app.Values", source)
                self.assertIn(".Subcharts.app.Chart", source)
                self.assertIn(".Release", source)
                self.assertNotIn("apiVersion:", source)
                self.assertNotIn("kind:", source)

    def test_all_24_profiles_keep_git_releases_and_flat_image_automation_keys(self):
        for component in COMPONENTS:
            for environment in ENVIRONMENTS:
                with self.subTest(component=component, environment=environment):
                    values = profile(component, environment)
                    release = values["_release"]
                    self.assertRegex(release["revision"], r"^[0-9a-f]{40}$")
                    self.assertNotIn("repository", release)
                    self.assertEqual(release["namespace"], "playground-" + environment)
                    self.assertEqual(release["sourceRepository"], "anomaly51/playground-" + component)
                    self.assertEqual(values["image"]["repository"],
                                     "harbor.internal.api-api-api.com/playground/" + component)
                    self.assertRegex(values["image"]["tag"], r"^[^@]+@sha256:[0-9a-f]{64}$")
                    self.assertIn("env", values)
                    self.assertNotIn("image", values.get("app", {}))

    def test_all_stable_profiles_render_only_the_existing_resource_identities(self):
        for component in COMPONENTS:
            names = {component, "outbox-relay"} if component == "order-service" else {component}
            for environment in ENVIRONMENTS:
                with self.subTest(component=component, environment=environment):
                    documents = render(component, environment)
                    expected = {(kind, name) for kind in ("Service", "Deployment") for name in names}
                    identities = [(doc["kind"], doc["metadata"]["name"]) for doc in documents]
                    self.assertEqual(set(identities), expected)
                    self.assertEqual(len(identities), len(expected), "Dependency must not emit duplicate workloads")
                    for document in documents:
                        self.assertEqual(document["metadata"]["namespace"], "playground-" + environment)
                        self.assertEqual(document["metadata"]["labels"], {
                            "app.kubernetes.io/name": document["metadata"]["name"],
                            "app.kubernetes.io/part-of": "playground",
                        })

    def test_flat_image_env_and_scheduling_overrides_reach_main_and_relay(self):
        for component in COMPONENTS:
            with self.subTest(component=component):
                documents = render(component, "staging", override=True)
                for deployment in (d for d in documents if d["kind"] == "Deployment"):
                    pod = deployment["spec"]["template"]["spec"]
                    self.assertEqual(pod["nodeSelector"], {"kubernetes.io/hostname": "contract-worker"})
                    container = pod["containers"][0]
                    self.assertEqual(container["image"], "example.invalid/contract-image:pinned")
                    env = {item["name"]: item.get("value") for item in container["env"]}
                    self.assertEqual(env["REUSABLE_CHART_CONTRACT"], "root-values")

    def test_all_eight_preview_profiles_keep_release_selectors_and_rollout_policy(self):
        for component in COMPONENTS:
            with self.subTest(component=component):
                documents = render(component, "staging", override=True, ephemeral=True)
                services = {d["metadata"]["name"]: d for d in documents if d["kind"] == "Service"}
                for deployment in (d for d in documents if d["kind"] == "Deployment"):
                    name = deployment["metadata"]["name"]
                    selector = {"app.kubernetes.io/name": name, "app.kubernetes.io/instance": component}
                    self.assertEqual(deployment["spec"]["selector"]["matchLabels"], selector)
                    self.assertEqual(services[name]["spec"]["selector"], selector)
                    self.assertEqual(deployment["metadata"]["namespace"], "playground-preview-contract-0123456789")
                    self.assertEqual(deployment["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"], "3")
                    self.assertEqual(deployment["spec"]["strategy"], {
                        "type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1, "maxSurge": 0}})

    def test_actual_profiles_still_promote_only_root_image_and_git_revision(self):
        for component in COMPONENTS:
            with self.subTest(component=component):
                production = profile(component, "prod")
                staging = profile(component, "staging")
                promoted = promote_playground.promoted_values(
                    production, staging, component, "a" * 40, "b" * 40)
                self.assertEqual(promoted["image"]["tag"], staging["image"]["tag"])
                self.assertEqual(promoted["_release"]["revision"], staging["_release"]["revision"])
                self.assertEqual(promoted["_release"]["imageKeys"], ["image"])
                self.assertEqual(promoted["_release"]["namespace"], "playground-prod")
                for key in production.keys() - {"image", "_release"}:
                    self.assertEqual(promoted[key], production[key], key)


if __name__ == "__main__":
    unittest.main()
