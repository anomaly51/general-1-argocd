"""Render playground charts with the values actually supplied by ApplicationSet."""

from pathlib import Path
import subprocess
import unittest

import yaml


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
                chart = ROOT / f"apps/playground-{component}"
                profile = yaml.safe_load((chart / "values/prod.yaml").read_text())
                namespace = profile["_release"]["namespace"]
                inline_values = {key: value for key, value in profile.items()
                                 if key not in {"_release", "path"}}
                result = subprocess.run(
                    ["helm", "template", f"playground-{component}", str(chart),
                     "--namespace", namespace, "--values", "-"],
                    input=yaml.safe_dump(inline_values), text=True, capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                documents = [doc for doc in yaml.safe_load_all(result.stdout) if doc]
                self.assertTrue(documents)
                for document in documents:
                    self.assertEqual(document["metadata"]["namespace"], namespace)


if __name__ == "__main__":
    unittest.main()
