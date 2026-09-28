import copy
from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("environments", ROOT / "scripts/environments.py")
env = importlib.util.module_from_spec(spec)
spec.loader.exec_module(env)


class EnvironmentsTest(unittest.TestCase):
    def setUp(self):
        self.stage = env.read(env.descriptor_path(ROOT, "shisha-guide", "staging"))
        self.prod = env.read(env.descriptor_path(ROOT, "shisha-guide", "prod"))

    def test_disabled_staging_cannot_be_promoted(self):
        self.stage["enabled"] = "false"
        with self.assertRaisesRegex(ValueError, "Staging is disabled"):
            env.promote_release(self.stage, self.prod)

    def test_promotes_all_component_versions_and_preserves_prod_identity(self):
        self.stage["enabled"] = "true"
        for c in self.stage["components"]:
            c["chartRevision"] = "a" * 40
        self.stage["components"][0]["releaseValues"]["images"]["api"]["tag"] = "sha-tested"
        before = copy.deepcopy(self.prod)
        result = env.promote_release(self.stage, self.prod)
        self.assertEqual(self.prod, before)
        self.assertEqual(result["namespace"], before["namespace"])
        self.assertEqual(result["environment"], "prod")
        for old, new, source in zip(before["components"], result["components"], self.stage["components"]):
            for key in ("applicationName", "releaseName", "component", "environmentValues"):
                self.assertEqual(new[key], old[key])
            for key in env.RELEASE_FIELDS:
                self.assertEqual(new[key], source[key])

    def test_missing_component_blocks_partial_product_promotion(self):
        self.stage["enabled"] = "true"
        self.stage["components"].pop()
        with self.assertRaisesRegex(ValueError, "same components"):
            env.promote_release(self.stage, self.prod)

    def test_different_chart_identity_is_not_a_release(self):
        self.stage["enabled"] = "true"
        self.stage["components"][0]["chartPath"] = "apps/another-app"
        with self.assertRaisesRegex(ValueError, "chart identity"):
            env.promote_release(self.stage, self.prod)

    def test_path_traversal_rejected(self):
        with self.assertRaisesRegex(ValueError, "product"):
            env.descriptor_path(ROOT, "../../../cluster", "prod")

    def test_floating_chart_revision_rejected(self):
        path = env.descriptor_path(ROOT, "shisha-guide", "prod")
        self.prod["components"][0]["chartRevision"] = "main"
        with self.assertRaisesRegex(ValueError, "full commit SHA"):
            env.validate_descriptor(self.prod, path, ROOT)

    def test_prod_namespace_cannot_be_used_for_staging(self):
        self.stage["namespace"] = "apps"
        path = env.descriptor_path(ROOT, "shisha-guide", "staging")
        with self.assertRaisesRegex(ValueError, "own product/environment namespace"):
            env.validate_descriptor(self.stage, path, ROOT)

    def test_existing_inventory_is_valid(self):
        self.assertTrue(env.inventory(ROOT))

    def test_cli_promotes_selected_snapshot_without_touching_prod_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            stage_path = env.descriptor_path(root, "shisha-guide", "staging")
            prod_path = env.descriptor_path(root, "shisha-guide", "prod")
            for original, target in ((env.descriptor_path(ROOT, "shisha-guide", "staging"), stage_path),
                                     (env.descriptor_path(ROOT, "shisha-guide", "prod"), prod_path)):
                target.parent.mkdir(parents=True)
                for file in original.parent.glob("*.yaml"):
                    target.with_name(file.name).write_bytes(file.read_bytes())
            self.stage["enabled"] = "true"
            self.stage["components"][0]["chartRevision"] = "a" * 40
            stage_path.write_text(env.yaml.safe_dump(self.stage))

            def git(*args):
                return env.git(root, *args)

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.invalid")
            git("add", ".")
            git("commit", "-qm", "Tested staging")
            selected = git("rev-parse", "HEAD")
            self.stage["components"][0]["chartRevision"] = "b" * 40
            stage_path.write_text(env.yaml.safe_dump(self.stage))
            git("add", ".")
            git("commit", "-qm", "Newer staging")
            settings = {p.name: p.read_bytes() for p in prod_path.parent.glob("*.yaml")}
            with redirect_stdout(io.StringIO()):
                env.promote(root, "shisha-guide", selected, dry_run=True)
            self.assertEqual(prod_path.read_bytes(), settings["deployment.yaml"])
            with redirect_stdout(io.StringIO()):
                env.promote(root, "shisha-guide", selected, dry_run=False)
            result = env.read(prod_path)
            self.assertEqual(result["components"][0]["chartRevision"], "a" * 40)
            self.assertEqual(result["promotedFrom"], selected)
            for old, new in zip(self.prod["components"], result["components"]):
                self.assertEqual(old["environmentValues"], new["environmentValues"])
            for name, content in settings.items():
                if name != "deployment.yaml":
                    self.assertEqual(prod_path.with_name(name).read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
