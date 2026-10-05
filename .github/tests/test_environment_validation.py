"""The public chart mirror keeps untrusted PR validation off the LAN runners."""
import os
from pathlib import Path
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[2]


class EnvironmentValidation(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load((ROOT / ".github/workflows/validate-environments.yaml").read_text())
        self.job = self.workflow["jobs"]["validate"]

    def test_pr_validation_uses_public_pinned_chart_without_privileged_runner(self):
        self.assertEqual(self.job["runs-on"], "ubuntu-latest")
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        fetch = next(step for step in self.job["steps"]
                     if step.get("name") == "Fetch pinned public app chart sources")
        self.assertIn('"https://github.com/anomaly51/platform-helm-charts.git"', fetch["run"])
        self.assertIn('f"app-v{version}"', fetch["run"])
        self.assertIn('validate_chart_pin(release)', fetch["run"])
        self.assertNotIn("secrets.", fetch["run"])
        self.assertNotIn("0.6.0", fetch["run"])
        self.assertEqual(self.job["env"]["APP_CHART_PATH"],
                         "${{ github.workspace }}/.ci/app-charts/{version}/charts/app")

    @unittest.skipUnless(os.environ.get("APP_CHART_PATH"), "Set APP_CHART_PATH to the pinned shared chart")
    def test_workflow_validates_profiles_without_local_playground_charts(self):
        step = next(step for step in self.job["steps"]
                    if step.get("name") == "Validate automatically discovered environments")
        code = step["run"].removeprefix("python - <<'PYCODE'\n").removesuffix("PYCODE\n")
        original = Path.cwd()
        try:
            os.chdir(ROOT)
            exec(compile(code, "validate-environments.yaml", "exec"), {})
        finally:
            os.chdir(original)


if __name__ == "__main__":
    unittest.main()
