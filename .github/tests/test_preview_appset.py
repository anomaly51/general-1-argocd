"""The preview ApplicationSet only materializes trusted active GitOps records."""

from copy import deepcopy
from pathlib import Path
import subprocess
import tempfile
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
APPSET_PATH = ROOT / "cluster/applicationsets/playground-previews.yaml"
REPOSITORY = "https://github.com/anomaly51/general-1-argocd.git"
COMPONENTS = (
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
)
UTILITIES = ("namespace", "kafka", "postgres", "mysql", "rabbitmq", "redis", "edge")


def fixture():
    sources = []
    for component in COMPONENTS:
        sources.append({
            "repoURL": "harbor.internal.api-api-api.com/helm-charts", "targetRevision": "0.6.0",
            "chart": "app",
            "helm": {"releaseName": component, "valuesObject":
                     {"image": {"repository": f"harbor.internal.api-api-api.com/playground/{component}",
                                "tag": "preview@sha256:" + "b" * 64}}},
        })
    for component in UTILITIES:
        sources.append({
            "repoURL": REPOSITORY, "targetRevision": "c" * 40,
            "path": f"utility-apps/playground-staging/{component}",
            "helm": {"releaseName": component, "values": "ephemeral: true\n"},
        })
    return {
        "name": "playground-preview-discounts-01234567",
        "namespace": "playground-preview-discounts-01234567",
        "branch": "feature/discounts",
        "url": "https://playground-preview-discounts-01234567.internal.api-api-api.com",
        "sources": sources,
    }


def render_go_templates(appset, parameters):
    # Helm's tpl exposes the same Go-template/Sprig functions used here. Render
    # the actual template and patch rather than approximating sources with a
    # string replacement; all source arrays and multiline values must survive.
    with tempfile.TemporaryDirectory(prefix="preview-appset-test-") as directory:
        chart = Path(directory)
        (chart / "templates").mkdir()
        (chart / "Chart.yaml").write_text("apiVersion: v2\nname: preview-test\nversion: 0.1.0\n")
        (chart / "templates/render.yaml").write_text(
            '{{ tpl .Values.application .Values.parameters }}\n---\n'
            '{{ tpl .Values.patch .Values.parameters }}\n'
        )
        values = {
            "application": yaml.safe_dump(appset["spec"]["template"]),
            "patch": appset["spec"]["templatePatch"],
            "parameters": parameters,
        }
        output = subprocess.run(
            ["helm", "template", "preview-test", str(chart), "--values", "-"],
            input=yaml.safe_dump(values), text=True, capture_output=True, check=True,
        ).stdout
    documents = [document for document in yaml.safe_load_all(output) if document]
    application = documents[0]

    def merge(target, patch):
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                merge(target[key], value)
            else:
                target[key] = deepcopy(value)

    for patch in documents[1:]:
        merge(application, patch)
    return application


class PreviewApplicationSetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.appset = yaml.safe_load(APPSET_PATH.read_text())

    def test_discovery_is_only_active_json_records_from_trusted_main(self):
        self.assertEqual(self.appset["metadata"], {"name": "playground-previews", "namespace": "argocd"})
        spec = self.appset["spec"]
        self.assertTrue(spec["goTemplate"])
        self.assertEqual(spec["goTemplateOptions"], ["missingkey=error"])
        self.assertEqual(spec["generators"], [{"git": {
            "repoURL": REPOSITORY, "revision": "main", "requeueAfterSeconds": 30,
            "files": [{"path": "previews/active/*.json"}],
        }}])
        # An absent or empty active directory yields no matches. Configuration
        # and expired records cannot accidentally create an Application.
        pattern = spec["generators"][0]["git"]["files"][0]["path"]
        self.assertFalse(Path("previews/config.json").match(pattern))
        self.assertFalse(Path("previews/state/expired.json").match(pattern))
        self.assertTrue(Path("previews/active/discounts.json").match(pattern))

    def test_full_stack_sources_render_without_losing_pins_or_inline_values(self):
        parameters = fixture()
        rendered = render_go_templates(self.appset, parameters)
        self.assertEqual(rendered["metadata"]["name"], parameters["name"])
        self.assertEqual(rendered["metadata"]["namespace"], "argocd")
        self.assertEqual(rendered["metadata"]["annotations"]["preview.api-api-api.com/branch"], parameters["branch"])
        self.assertEqual(rendered["metadata"]["annotations"]["preview.api-api-api.com/url"], parameters["url"])
        self.assertEqual(rendered["spec"]["project"], "gitops-apps")
        self.assertEqual(rendered["spec"]["destination"], {
            "server": "https://kubernetes.default.svc", "namespace": parameters["namespace"],
        })
        self.assertEqual(rendered["spec"]["sources"], parameters["sources"])
        self.assertEqual(len(rendered["spec"]["sources"]), 15)
        self.assertNotIn("source", rendered["spec"])

    def test_different_feature_records_produce_independent_namespaces(self):
        first = fixture()
        second = deepcopy(first)
        second.update(name="playground-preview-checkout-abcdef12",
                      namespace="playground-preview-checkout-abcdef12", branch="feature/checkout",
                      url="https://playground-preview-checkout-abcdef12.internal.api-api-api.com")
        first_app = render_go_templates(self.appset, first)
        second_app = render_go_templates(self.appset, second)
        self.assertNotEqual(first_app["metadata"]["name"], second_app["metadata"]["name"])
        self.assertNotEqual(first_app["spec"]["destination"], second_app["spec"]["destination"])
        self.assertNotIn("maxUpdate", yaml.safe_dump(self.appset))
        self.assertNotIn("rollingSync", yaml.safe_dump(self.appset))

    def test_removing_active_record_cascades_resources_without_preservation(self):
        spec = self.appset["spec"]
        self.assertEqual(spec["syncPolicy"], {
            "applicationsSync": "sync", "preserveResourcesOnDeletion": False,
        })
        self.assertEqual(spec["template"]["metadata"]["finalizers"], [
            "resources-finalizer.argocd.argoproj.io",
        ])
        sync = spec["template"]["spec"]["syncPolicy"]
        self.assertEqual(sync["automated"], {"prune": True, "selfHeal": True, "allowEmpty": True})
        self.assertEqual(sync["syncOptions"], ["CreateNamespace=true", "ServerSideApply=true"])
        self.assertEqual(sync["retry"], {"limit": 1,
                         "backoff": {"duration": "5s", "factor": 1, "maxDuration": "5s"}})
        self.assertNotIn("Prune=false", yaml.safe_dump(spec))
        self.assertNotIn("Delete=false", yaml.safe_dump(spec))

    def test_expiry_and_images_are_owned_by_central_lifecycle_not_appset(self):
        text = APPSET_PATH.read_text()
        for unrelated in ("image-updater", "ttlSeconds", "expiresAt", "pullRequest:", "schedule:"):
            self.assertNotIn(unrelated, text)
        self.assertNotIn("managedNamespaceMetadata", self.appset["spec"]["template"]["spec"]["syncPolicy"])

    def test_root_kustomization_includes_preview_appset_exactly_once(self):
        resources = yaml.safe_load((ROOT / "cluster/kustomization.yaml").read_text())["resources"]
        self.assertEqual(resources.count("applicationsets/playground-previews.yaml"), 1)
        self.assertIn("applicationsets/apps.yaml", resources)
        self.assertIn("applicationsets/utility-apps.yaml", resources)


if __name__ == "__main__":
    unittest.main()
