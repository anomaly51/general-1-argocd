"""Render playground charts with the values actually supplied by ApplicationSet."""

from pathlib import Path
import subprocess
import unittest

import yaml

from test_reusable_app import chart_arguments


ROOT = Path(__file__).resolve().parents[2]
COMPONENTS = (
    "order-service", "pricing-service", "inventory-service", "event-hub",
    "analytics-service", "shell", "topology-mfe", "traffic-mfe",
)


class PlaygroundApplicationSetValuesTests(unittest.TestCase):
    def test_all_charts_render_without_release_metadata_in_inline_values(self):
        appset = yaml.safe_load((ROOT / "cluster/applicationsets/apps.yaml").read_text())
        source = appset["spec"]["template"]["spec"]["source"]
        self.assertEqual(source["helm"]["values"], '{{ omit . "_release" "path" | toJson }}')
        self.assertNotIn("valueFiles", source["helm"])
        for component in COMPONENTS:
            with self.subTest(component=component):
                values_dir = ROOT / f"apps/playground-{component}/values"
                profile = yaml.safe_load((values_dir / "prod.yaml").read_text())
                chart, extra = chart_arguments(profile["_release"])
                namespace = profile["_release"]["namespace"]
                inline_values = {key: value for key, value in profile.items()
                                 if key not in {"_release", "path"}}
                result = subprocess.run(
                    ["helm", "template", f"playground-{component}", str(chart), *extra,
                     "--namespace", namespace, "--values", "-"],
                    input=yaml.safe_dump(inline_values), text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
                self.assertTrue(documents)
                for document in documents:
                    self.assertEqual(document["metadata"]["namespace"], namespace)

    def test_optional_worker_selection_does_not_change_production_defaults(self):
        for component in COMPONENTS:
            values_dir = ROOT / f"apps/playground-{component}/values"
            values = yaml.safe_load((values_dir / "prod.yaml").read_text())
            chart, extra = chart_arguments(values["_release"])
            for selected in (False, True):
                with self.subTest(component=component, selected=selected):
                    inline = {key: value for key, value in values.items() if key != "_release"}
                    if selected:
                        inline["nodeSelector"] = {"kubernetes.io/hostname": "general-1-worker-2"}
                    rendered = subprocess.run(
                        ["helm", "template", f"playground-{component}", str(chart), *extra,
                         "--namespace", "playground-dev", "--values", "-"],
                        input=yaml.safe_dump(inline), text=True, capture_output=True, check=True,
                    )
                    deployments = [doc for doc in yaml.safe_load_all(rendered.stdout)
                                   if doc and doc["kind"] == "Deployment"]
                    self.assertEqual(len(deployments), 2 if component == "order-service" else 1)
                    for deployment in deployments:
                        pod = deployment["spec"]["template"]["spec"]
                        if selected:
                            self.assertEqual(pod["nodeSelector"], inline["nodeSelector"])
                        else:
                            self.assertNotIn("nodeSelector", pod)


if __name__ == "__main__":
    unittest.main()
