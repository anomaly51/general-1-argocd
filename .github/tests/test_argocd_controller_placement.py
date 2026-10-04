"""The controller move owns only scheduling and resource requests, not bootstrap."""

from pathlib import Path
import subprocess
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "utility-apps/argocd/controller-placement"


class ControllerPlacementTests(unittest.TestCase):
    def test_chart_renders_only_the_partial_statefulset_intent(self):
        output = subprocess.check_output(
            ["helm", "template", "controller-placement", str(CHART), "--namespace", "argocd"],
            text=True,
        )
        documents = [doc for doc in yaml.safe_load_all(output) if doc]
        self.assertEqual(len(documents), 1)
        doc = documents[0]
        self.assertEqual((doc["apiVersion"], doc["kind"]), ("apps/v1", "StatefulSet"))
        self.assertEqual(doc["metadata"], {
            "name": "argocd-application-controller", "namespace": "argocd",
            "annotations": {"argocd.argoproj.io/sync-options":
                            "ServerSideApply=true,Validate=false,Prune=false,Delete=false"},
        })
        self.assertEqual(doc["spec"], {"template": {"spec": {
            "nodeSelector": {"kubernetes.io/os": "linux", "kubernetes.io/hostname": "general-1-worker-2"},
            "containers": [{"name": "argocd-application-controller",
                            "resources": {"requests": {"cpu": "100m", "memory": "512Mi"}}}],
        }}})
        # Exact equality rejects image/env/replica/volume changes or new hard limits.
        self.assertNotIn("Job", output)

    def test_only_this_application_disables_bootstrap_field_migration(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/utility-apps.yaml").read_text())
        template = appset["spec"]["template"]
        self.assertEqual(template["metadata"]["annotations"]["argocd.argoproj.io/compare-options"],
                         "ServerSideDiff=true")
        self.assertEqual(template["spec"]["syncPolicy"]["syncOptions"],
                         ["CreateNamespace=true", "ServerSideApply=true"])
        patch = appset["spec"]["templatePatch"]
        self.assertIn('if eq .path.path "utility-apps/argocd/controller-placement"', patch)
        self.assertEqual(patch.count("{{- if"), 1)
        self.assertIn("ClientSideApplyMigration=false", patch)
        self.assertIn("Validate=false", patch)
        self.assertNotIn("DisableClientSideApplyMigration", patch)
        self.assertNotIn("Replace=true", patch)
        self.assertNotIn("Force=true", patch)


if __name__ == "__main__":
    unittest.main()
