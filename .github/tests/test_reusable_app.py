"""Contracts for direct OCI app releases and values-only application folders.

Hosted CI sets APP_CHART_PATH to the released public chart source; local runs
can render the exact OCI release directly. Neither uses per-app chart copies.
"""

from functools import lru_cache
import os
from pathlib import Path
import subprocess
import sys
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import promote_playground
from test_preview_appset import render_go_templates


COMPONENTS = (
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
)
ENVIRONMENTS = ("dev", "staging", "prod")
REGISTRY = "harbor.internal.api-api-api.com/helm-charts"


def profile(component, environment):
    return yaml.safe_load((ROOT / f"apps/playground-{component}/values/{environment}.yaml").read_text())


def chart_arguments(pin):
    chart = os.environ.get("APP_CHART_PATH")
    if chart:
        chart = chart.format(version=pin["revision"])
        metadata = yaml.safe_load((Path(chart) / "Chart.yaml").read_text())
        assert (metadata["name"], metadata["version"]) == (pin["chart"], pin["revision"])
        return chart, []
    return f"oci://{pin['repository']}/{pin['chart']}", ["--version", pin["revision"]]


@lru_cache(maxsize=None)
def render(component, environment, override=False, ephemeral=False):
    values = profile(component, environment)
    pin = values.pop("_release")
    namespace = pin["namespace"]
    chart, extra = chart_arguments(pin)
    if override:
        values["image"] = {"repository": "example.invalid/contract-image", "tag": "pinned"}
        values.setdefault("env", {})["REUSABLE_CHART_CONTRACT"] = "root-values"
        values["nodeSelector"] = {"kubernetes.io/hostname": "contract-worker"}
    if ephemeral:
        values["ephemeral"] = True
        namespace = "playground-preview-contract-0123456789"
    release = component if ephemeral else "playground-" + component
    result = subprocess.run(
        ["helm", "template", release, chart,
         "--namespace", namespace, "--values", "-", *extra],
        input=yaml.safe_dump(values), text=True, capture_output=True,
    )
    if result.returncode:
        raise AssertionError(f"{component}/{environment}: Helm failed:\n{result.stderr}")
    return [document for document in yaml.safe_load_all(result.stdout) if document]


class ReusableAppReleaseTests(unittest.TestCase):
    def test_public_oci_source_is_registered_without_credentials(self):
        registry = yaml.safe_load((ROOT / "cluster/platform-helm-repository.yaml").read_text())
        self.assertEqual(registry["metadata"]["labels"]["argocd.argoproj.io/secret-type"], "repository")
        self.assertEqual(registry["stringData"], {
            "name": "platform-helm-charts", "type": "helm", "url": REGISTRY,
            "project": "gitops-apps", "enableOCI": "true"})
        resources = yaml.safe_load((ROOT / "cluster/kustomization.yaml").read_text())["resources"]
        self.assertEqual(resources.count("platform-helm-repository.yaml"), 1)

    def test_other_applications_keep_their_existing_git_or_oci_source(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/apps.yaml").read_text())
        for path in sorted((ROOT / "apps").glob("*/values/prod.yaml")):
            name = path.parent.parent.name
            if name.startswith("playground-"):
                continue
            with self.subTest(application=name):
                values = yaml.safe_load(path.read_text())
                release = values["_release"]
                parameters = {**values, "path": {"filename": "prod.yaml",
                              "segments": ["apps", name, "values"]}}
                source = render_go_templates(appset, parameters)["spec"]["source"]
                self.assertEqual(source["targetRevision"], release["revision"])
                if "repository" in release:
                    self.assertEqual(source["repoURL"], release["repository"])
                    self.assertEqual(source["chart"], release.get("chart", name))
                    self.assertFalse(source.get("path"))
                else:
                    self.assertEqual(source["repoURL"], "https://github.com/anomaly51/general-1-argocd.git")
                    self.assertEqual(source["path"], "apps/" + name)
                    self.assertFalse(source.get("chart"))

    def test_application_folders_contain_only_environment_values(self):
        for component in COMPONENTS:
            with self.subTest(component=component):
                folder = ROOT / f"apps/playground-{component}"
                files = {str(path.relative_to(folder)) for path in folder.rglob("*") if path.is_file()}
                self.assertEqual(files, {f"values/{environment}.yaml" for environment in ENVIRONMENTS})

    def test_appset_points_directly_to_oci_with_exact_values_and_release_names(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/apps.yaml").read_text())
        for component in COMPONENTS:
            for environment in ENVIRONMENTS:
                with self.subTest(component=component, environment=environment):
                    values = profile(component, environment)
                    parameters = {**values, "path": {"filename": environment + ".yaml",
                                  "segments": ["apps", "playground-" + component, "values"]}}
                    application = render_go_templates(appset, parameters)
                    source = application["spec"]["source"]
                    self.assertEqual(source["repoURL"], REGISTRY)
                    self.assertEqual(source["chart"], "app")
                    self.assertEqual(source["targetRevision"], values["_release"]["revision"])
                    self.assertFalse(source.get("path"))
                    self.assertEqual(source["helm"]["releaseName"], "playground-" + component)
                    self.assertEqual(yaml.safe_load(source["helm"]["values"]),
                                     {key: value for key, value in values.items() if key != "_release"})
                    automated = application["spec"]["syncPolicy"].get("automated")
                    self.assertEqual(automated, None if environment == "prod" else
                                     {"prune": True, "selfHeal": True})

    def test_all_24_profiles_pin_oci_and_keep_flat_image_automation_keys(self):
        for component in COMPONENTS:
            for environment in ENVIRONMENTS:
                with self.subTest(component=component, environment=environment):
                    values = profile(component, environment)
                    release = values["_release"]
                    self.assertRegex(release["revision"], r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?$")
                    self.assertEqual(release["repository"], REGISTRY)
                    self.assertEqual(release["chart"], "app")
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

    def test_actual_profiles_promote_image_and_chart_version_retaining_prod_settings(self):
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
