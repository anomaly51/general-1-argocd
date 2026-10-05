import copy
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import yaml

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import gitops_release as release
import argocd_release as argo
import promote_release as promote


class ReleaseGuards(unittest.TestCase):
    def setUp(self):
        self.values = {"_release": {"revision": "a" * 40, "namespace": "example-dev"},
                       "image": {"repository": "harbor.internal.api-api-api.com/applications/example",
                                 "tag": "old", "pullPolicy": "IfNotPresent"},
                       "vault": {"envPath": "apps/example/dev"}, "replicas": 2}
        self.source = {"commit": "b" * 40, "branch": "dev", "event": "push", "repository": "anomaly51/example",
                       "run_url": "https://github.com/anomaly51/example/actions/runs/1"}
        self.images = [{"key": "image", "repository": self.values["image"]["repository"],
                        "tag": "sha-" + "b" * 12, "digest": "sha256:" + "c" * 64}]

    def test_release_preserves_environment_settings_and_pins_digest(self):
        result = release.updated_profile(self.values, self.images, self.source, "d" * 40)
        self.assertEqual(result["vault"], self.values["vault"])
        self.assertEqual(result["replicas"], 2)
        self.assertEqual(result["_release"]["namespace"], "example-dev")
        self.assertEqual(result["image"]["pullPolicy"], "IfNotPresent")
        self.assertEqual(result["image"]["tag"], self.images[0]["tag"] + "@" + self.images[0]["digest"])
        self.assertEqual(self.values["image"]["tag"], "old")

    def test_explicit_oci_chart_keeps_identity_and_requires_exact_version(self):
        self.values["_release"].update(repository="harbor.internal.api-api-api.com/helm-charts",
                                       chart="app", revision="0.6.0")
        result = release.updated_profile(self.values, self.images, self.source, "0.6.1")
        self.assertEqual(result["_release"]["chart"], "app")
        self.assertEqual(result["_release"]["repository"], self.values["_release"]["repository"])
        self.assertEqual(release.updated_profile(self.values, self.images, self.source, "0.8.0-rc.1")
                         ["_release"]["revision"], "0.8.0-rc.1")
        for revision in ("main", "latest", "0.6.*", "a" * 40, "00.6.0", "0.6.0-rc..1", "0.6.0-"):
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                release.updated_profile(self.values, self.images, self.source, revision)
        for name in ("../app", "app/other", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                release.chart_name("example", {**self.values["_release"], "chart": name})

    def test_oci_validation_renders_published_chart_not_local_or_ci_override(self):
        self.values["_release"].update(repository="harbor.internal.api-api-api.com/helm-charts",
                                       chart="app", revision="0.6.0")
        with patch.object(promote.subprocess, "run") as render, patch.dict(
                "os.environ", {"APP_CHART_PATH": "/untrusted/local/chart"}):
            promote.validate_chart("playground-shell", self.values)
        command = render.call_args.args[0]
        self.assertEqual(command[:4], ["helm", "template", "playground-shell",
                                      "oci://harbor.internal.api-api-api.com/helm-charts/app"])
        self.assertEqual(command[-2:], ["--version", "0.6.0"])
        self.assertTrue(render.call_args.kwargs["check"])

    def test_legacy_oci_defaults_to_application_chart_name(self):
        self.values["_release"].update(repository="example.com/charts", revision="1.2.3")
        with patch.object(promote.subprocess, "run") as render:
            promote.validate_chart("example", self.values)
        self.assertEqual(render.call_args.args[0][3], "oci://example.com/charts/example")

    def test_oci_release_verification_checks_repository_and_chart_identity(self):
        self.values["_release"].update(repository="harbor.internal.api-api-api.com/helm-charts",
                                       chart="app", revision="0.6.0")
        source = {"repoURL": self.values["_release"]["repository"], "chart": "app", "targetRevision": "0.6.0",
                  "helm": {"values": yaml.safe_dump({k: v for k, v in self.values.items() if k != "_release"})}}
        self.assertTrue(argo.matches({"spec": {"source": source}}, self.values, "example"))
        for key, value in (("repoURL", "example.com/foreign"), ("chart", "foreign"), ("targetRevision", "0.5.1")):
            with self.subTest(key=key):
                self.assertFalse(argo.matches({"spec": {"source": {**source, key: value}}}, self.values, "example"))
        app = {"spec": {"source": source}, "status": {"sync": {"status": "Synced", "comparedTo": {
            "source": copy.deepcopy(source)}}, "health": {"status": "Healthy"}}}
        self.assertTrue(argo.healthy(app))
        for key, value in (("repoURL", "example.com/foreign"), ("chart", "foreign"), ("path", "apps/foreign")):
            changed = copy.deepcopy(app)
            changed["status"]["sync"]["comparedTo"]["source"][key] = value
            with self.subTest(compared_field=key):
                self.assertFalse(argo.healthy(changed))

    def test_git_release_verification_checks_repository_and_path(self):
        source = {"repoURL": "https://github.com/anomaly51/general-1-argocd.git", "path": "apps/example",
                  "targetRevision": self.values["_release"]["revision"],
                  "helm": {"values": yaml.safe_dump({k: v for k, v in self.values.items() if k != "_release"})}}
        self.assertTrue(argo.matches({"spec": {"source": source}}, self.values, "example"))
        for key, value in (("repoURL", "https://github.com/foreign/repo.git"), ("path", "apps/foreign"), ("chart", "app")):
            with self.subTest(key=key):
                self.assertFalse(argo.matches({"spec": {"source": {**source, key: value}}}, self.values, "example"))

    def test_automatic_production_and_wrong_branch_environment_rejected_before_git(self):
        with patch.object(release, "deployment_policy", return_value={}):
            for environment in ["prod", "staging"]:
                with self.assertRaises(ValueError):
                    release.publish("example", environment, self.images, self.source, None)

    def test_web_app_branch_mapping_remains_manual_for_production(self):
        with patch.object(release, "deployment_policy", return_value={}):
            self.assertEqual(release.automatic_environment("example", "main", "push"), "staging")
            self.assertEqual(release.automatic_environment("example", "dev", "push"), "dev")
            self.assertEqual(release.automatic_environment("example", "feature", "pull_request"), "")

    def test_bot_only_accepts_main_push_from_its_own_repository(self):
        policy = {"policy": "prod-only", "sourceRepository": "anomaly51/example"}
        source = {**self.source, "branch": "main"}
        with patch.object(release, "deployment_policy", return_value=policy):
            self.assertEqual(release.automatic_environment("example", "main", "push"), "prod")
            release.validate_automatic_release("example", "prod", source)
            for branch, event in [("dev", "push"), ("staging", "push"), ("main", "workflow_dispatch")]:
                with self.assertRaises(ValueError):
                    release.automatic_environment("example", branch, event)
            self.assertEqual(release.automatic_environment("example", "feature", "pull_request"), "")
            for environment in ["dev", "staging"]:
                with self.assertRaises(ValueError):
                    release.validate_automatic_release("example", environment, source)
            with self.assertRaises(ValueError):
                release.validate_automatic_release("example", "prod", {**source, "repository": "anomaly51/other"})
            with patch.object(Path, "exists", return_value=True):
                with self.assertRaisesRegex(ValueError, "dev or staging"):
                    release.validate_automatic_release("example", "prod", source)

    def test_bot_policy_is_preserved_and_manual_promote_is_rejected(self):
        production = copy.deepcopy(self.values)
        production["_release"].update(policy="prod-only", sourceRepository="anomaly51/example")
        result = release.updated_profile(production, self.images, {**self.source, "branch": "main"}, "a" * 40)
        self.assertEqual(result["_release"]["policy"], "prod-only")
        self.assertEqual(result["vault"], production["vault"])
        with self.assertRaisesRegex(ValueError, "automatically"):
            promote.production_only("example", production, "b" * 40, None)
        with self.assertRaisesRegex(ValueError, "automatically"):
            promote.promoted_values(production, result, "c" * 40)

    def test_foreign_registry_floating_chart_and_bad_digest_are_rejected(self):
        for field, value in [("repository", "attacker.example/image"), ("tag", "latest"), ("digest", "latest")]:
            image = copy.deepcopy(self.images)
            image[0][field] = value
            with self.assertRaises(ValueError):
                release.updated_profile(self.values, image, self.source, "d" * 40)
        with self.assertRaises(ValueError):
            release.updated_profile(self.values, self.images, self.source, "main")

    def test_duplicate_component_and_path_traversal_are_rejected(self):
        with self.assertRaises(ValueError):
            release.updated_profile(self.values, self.images * 2, self.source, "d" * 40)
        with self.assertRaises(ValueError):
            release.profile("../other", "dev")
        with self.assertRaises(ValueError):
            release.image_at(self.values, "_release.revision")

    def test_old_healthy_status_is_not_release_verification(self):
        import yaml
        desired = {"targetRevision": "a" * 40, "helm": {"values": yaml.safe_dump({"image": {"tag": "new"}})}}
        app = {"spec": {"source": desired}, "status": {"sync": {"status": "Synced", "comparedTo": {
            "source": {"targetRevision": "a" * 40, "helm": {"values": "old"}}}}, "health": {"status": "Healthy"}}}
        self.assertFalse(argo.healthy(app))
        app["status"]["sync"]["comparedTo"]["source"] = desired
        self.assertTrue(argo.healthy(app))
        app["operation"] = {"sync": {}}
        self.assertFalse(argo.healthy(app))

    def test_promotion_retains_production_secrets_namespace_and_limits(self):
        stage = release.updated_profile(self.values, self.images, {**self.source, "branch": "main"}, "d" * 40)
        production = copy.deepcopy(self.values)
        production["_release"]["namespace"] = "production"
        production["vault"]["envPath"] = "apps/production"
        production["replicas"] = 3
        result = promote.promoted_values(production, stage, "e" * 40)
        self.assertEqual(result["replicas"], 3)
        self.assertEqual(result["vault"], production["vault"])
        self.assertEqual(result["_release"]["namespace"], "production")
        self.assertEqual(result["image"]["tag"], stage["image"]["tag"])
        stage["_release"]["sourceBranch"] = "dev"
        with self.assertRaises(ValueError):
            promote.promoted_values(production, stage, "e" * 40)

    def test_oci_promotion_rejects_chart_source_or_name_changes(self):
        self.values["_release"].update(repository="harbor.internal.api-api-api.com/helm-charts",
                                       chart="app", revision="0.6.0")
        stage = release.updated_profile(self.values, self.images, {**self.source, "branch": "main"}, "0.6.1")
        result = promote.promoted_values(self.values, stage, "e" * 40)
        self.assertEqual(result["_release"]["chart"], "app")
        self.assertEqual(result["_release"]["revision"], "0.6.1")
        for key, value in (("chart", "foreign"), ("repository", "example.com/foreign")):
            changed = copy.deepcopy(stage)
            changed["_release"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "explicit migration"):
                promote.promoted_values(self.values, changed, "e" * 40)

    def test_live_verification_rejects_scaled_zero_stale_or_wrong_images(self):
        image = "example/image:sha-123@sha256:" + "a" * 64
        deployment = {"metadata": {"generation": 2}, "spec": {"replicas": 1,
            "template": {"spec": {"containers": [{"image": image}]}}},
            "status": {"observedGeneration": 2, "replicas": 1, "updatedReplicas": 1,
                       "readyReplicas": 1, "availableReplicas": 1}}
        self.assertEqual(argo.deployment_ready(deployment, {image}), {image})
        for key, value in [("observedGeneration", 1), ("readyReplicas", 0), ("updatedReplicas", 0)]:
            changed = copy.deepcopy(deployment)
            changed["status"][key] = value
            self.assertFalse(argo.deployment_ready(changed, {image}))
        deployment["spec"]["replicas"] = 0
        self.assertFalse(argo.deployment_ready(deployment, {image}))


if __name__ == "__main__":
    unittest.main()
