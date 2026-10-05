"""Regression coverage for trusted playground CI-to-GitOps publication."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

import gitops_release as gitops
import publish_playground as publisher
from registry_release import Registry

SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40
CHART_SHA = "c" * 40
GITOPS_SHA = "d" * 40
IMAGE_DIGEST = "sha256:" + "e" * 64
OTHER_DIGEST = "sha256:" + "f" * 64


def source(service="shell", branch="main"):
    return {"repository": f"anomaly51/playground-{service}", "commit": SOURCE_SHA,
            "branch": branch, "event": "push",
            "run_url": f"https://github.com/anomaly51/playground-{service}/actions/runs/123"}


def artifact(service="shell", branch="main"):
    return {"service": service,
            "repository": f"harbor.internal.api-api-api.com/playground/{service}",
            "source_repository": f"anomaly51/playground-{service}",
            "source_commit": SOURCE_SHA, "source_branch": branch, "digest": IMAGE_DIGEST}


def profile():
    return {"_release": {"revision": CHART_SHA, "policy": "promote", "namespace": "playground-staging"},
            "image": {"repository": artifact()["repository"], "tag": "staging@" + OTHER_DIGEST},
            "replicaCount": 2, "env": {"PUBLIC_URL": "https://staging.example"}}


class CallerArtifactTests(unittest.TestCase):
    def test_all_services_and_trusted_branches_produce_exact_commit_pin(self):
        for service in sorted(publisher.SERVICES):
            for branch in ("dev", "main"):
                with self.subTest(service=service, branch=branch):
                    release = artifact(service, branch)
                    original = copy.deepcopy(release)
                    image = publisher.validate_release(service, release, source(service, branch))
                    self.assertEqual(image, {"key": "image", "repository": release["repository"],
                                             "tag": f"{branch}-{SOURCE_SHA}", "digest": IMAGE_DIGEST})
                    self.assertEqual(release, original)

    def test_unknown_service_or_wrong_caller_repository_is_rejected(self):
        for service, repository in (("unregistered", "anomaly51/playground-unregistered"),
                                    ("shell", "attacker/playground-shell"),
                                    ("shell", "anomaly51/playground-order-service")):
            with self.subTest(service=service, repository=repository):
                caller = {**source(), "repository": repository}
                with self.assertRaisesRegex(ValueError, "source repository"):
                    publisher.validate_release(service, artifact(), caller)

    def test_non_push_and_non_deployment_branches_are_rejected(self):
        for event, branch in (("pull_request", "main"), ("workflow_dispatch", "main"),
                              ("workflow_run", "dev"), ("push", "feature/test"),
                              ("push", "prod"), ("push", "refs/heads/main")):
            with self.subTest(event=event, branch=branch), self.assertRaisesRegex(ValueError, "trusted pushes"):
                publisher.validate_release("shell", artifact(), {**source(), "event": event, "branch": branch})

    def test_source_revision_requires_full_lowercase_sha(self):
        for sha in (SOURCE_SHA[:12], SOURCE_SHA.upper(), "main", "", SOURCE_SHA + "\n"):
            with self.subTest(sha=sha), self.assertRaisesRegex(ValueError, "full source commit"):
                publisher.validate_release("shell", artifact(), {**source(), "commit": sha})

    def test_every_artifact_identity_field_must_match_caller(self):
        alternatives = {"service": "order-service", "repository": "evil.example/playground/shell",
                        "source_repository": "anomaly51/playground-order-service",
                        "source_commit": OTHER_SHA, "source_branch": "dev"}
        for field, value in alternatives.items():
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "does not match"):
                publisher.validate_release("shell", {**artifact(), field: value}, source())

    def test_extra_and_missing_artifact_fields_are_rejected(self):
        for field in artifact():
            release = artifact()
            del release[field]
            with self.subTest(missing=field), self.assertRaisesRegex(ValueError, "does not match"):
                publisher.validate_release("shell", release, source())
        with self.assertRaisesRegex(ValueError, "does not match"):
            publisher.validate_release("shell", {**artifact(), "environment": "prod"}, source())

    def test_digest_requires_exact_sha256(self):
        for digest in ("latest", "sha256:abcd", "sha512:" + "a" * 128,
                       IMAGE_DIGEST.upper(), IMAGE_DIGEST + "\n", None, 123):
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, "verified image digest"):
                publisher.validate_release("shell", {**artifact(), "digest": digest}, source())


class PublisherEntryPointTests(unittest.TestCase):
    def run_publisher(self, branch="main", release=None, registry=None, publish=None):
        caller = source(branch=branch)
        environment = {"GITHUB_REPOSITORY": caller["repository"], "GITHUB_SHA": SOURCE_SHA,
                       "GITHUB_REF_NAME": branch, "GITHUB_EVENT_NAME": "push", "GITHUB_RUN_ID": "123"}
        registry = registry or Mock()
        publish = publish or Mock()
        if not registry.image.called and not isinstance(registry.image.return_value, tuple):
            registry.image.return_value = (IMAGE_DIGEST, caller["repository"])
        if not registry.manifest.called and not isinstance(registry.manifest.return_value, tuple):
            registry.manifest.return_value = ({}, IMAGE_DIGEST)
        with patch.dict(os.environ, environment), \
                patch("sys.argv", ["publish_playground.py", "--service", "shell", "--artifact", "/unused/release.json"]), \
                patch.object(Path, "read_text", return_value=json.dumps(release if release is not None else artifact(branch=branch))), \
                patch.object(publisher, "Registry", return_value=registry) as constructor, \
                patch.object(publisher, "publish", publish):
            publisher.main()
            return registry, constructor, publish

    def test_dev_and_main_target_only_their_environment_and_preserve_chart(self):
        for branch, environment in (("dev", "dev"), ("main", "staging")):
            with self.subTest(branch=branch):
                registry, constructor, publish = self.run_publisher(branch)
                constructor.assert_called_once_with(artifact()["repository"])
                registry.image.assert_called_once_with(IMAGE_DIGEST, SOURCE_SHA,
                                                       require_main_label=False, source_branch=branch)
                registry.manifest.assert_called_once_with(f"{branch}-{SOURCE_SHA}")
                publish.assert_called_once_with("playground-shell", environment,
                    [{"key": "image", "repository": artifact()["repository"],
                      "tag": f"{branch}-{SOURCE_SHA}", "digest": IMAGE_DIGEST}],
                    source(branch=branch), None, preserve_chart=True)

    def test_invalid_artifact_never_queries_registry_or_publishes(self):
        registry, publish = Mock(), Mock()
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.run_publisher(release={**artifact(), "source_commit": OTHER_SHA}, registry=registry, publish=publish)
        registry.image.assert_not_called()
        registry.manifest.assert_not_called()
        publish.assert_not_called()

    def test_digest_or_repository_provenance_mismatch_cannot_publish(self):
        for digest, repository in ((OTHER_DIGEST, source()["repository"]),
                                   (IMAGE_DIGEST, "anomaly51/playground-order-service")):
            registry = Mock()
            publish = Mock()
            registry.image.return_value = (digest, repository)
            with self.subTest(digest=digest, repository=repository), \
                    self.assertRaisesRegex(ValueError, "Registry provenance"):
                self.run_publisher(registry=registry, publish=publish)
            registry.manifest.assert_not_called()
            publish.assert_not_called()

    def test_immutable_tag_must_resolve_to_the_recorded_digest(self):
        registry = Mock()
        publish = Mock()
        registry.manifest.return_value = ({}, OTHER_DIGEST)
        with self.assertRaisesRegex(ValueError, "tag no longer resolves"):
            self.run_publisher(registry=registry, publish=publish)
        publish.assert_not_called()


class GitOpsProfileTests(unittest.TestCase):
    def test_full_commit_pin_preserves_configuration_and_chart_metadata(self):
        values = profile()
        original = copy.deepcopy(values)
        image = publisher.validate_release("shell", artifact(), source())
        result = gitops.updated_profile(values, [image], source(), CHART_SHA)
        self.assertEqual(values, original)
        self.assertEqual(result["image"]["tag"], "main-" + SOURCE_SHA + "@" + IMAGE_DIGEST)
        self.assertEqual(result["env"], original["env"])
        self.assertEqual(result["replicaCount"], 2)
        self.assertEqual(result["_release"]["revision"], CHART_SHA)
        self.assertEqual(result["_release"]["policy"], "promote")
        self.assertEqual(result["_release"]["namespace"], "playground-staging")
        self.assertEqual(result["_release"]["sourceCommit"], SOURCE_SHA)
        self.assertEqual(result["_release"]["sourceBranch"], "main")

    def test_profile_rejects_other_branch_or_commit_tag(self):
        for tag in ("dev-" + SOURCE_SHA, "main-" + OTHER_SHA, "main-" + SOURCE_SHA[:12], "staging"):
            image = publisher.validate_release("shell", artifact(), source())
            image["tag"] = tag
            with self.subTest(tag=tag), self.assertRaisesRegex(ValueError, "exact source commit"):
                gitops.updated_profile(profile(), [image], source(), CHART_SHA)

    def test_publish_preserve_chart_does_not_advance_git_chart_pin(self):
        with tempfile.TemporaryDirectory(prefix="playground-publish-test-") as directory:
            path = Path(directory) / "staging.yaml"
            path.write_text(yaml.safe_dump(profile()))
            response = io.StringIO(json.dumps({"object": {"sha": SOURCE_SHA}}))
            with patch.object(gitops, "validate_automatic_release"), \
                    patch.object(gitops, "profile", return_value=path), \
                    patch.dict(os.environ, {"GH_TOKEN": "test-token"}), \
                    patch.object(gitops.urllib.request, "urlopen", return_value=response), \
                    patch.object(gitops.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as commands, \
                    patch.object(gitops, "git", return_value=GITOPS_SHA), \
                    patch.object(gitops, "release_output"), patch("builtins.print"):
                gitops.publish("playground-shell", "staging",
                               [publisher.validate_release("shell", artifact(), source())],
                               source(), None, preserve_chart=True)
            self.assertEqual(yaml.safe_load(path.read_text())["_release"]["revision"], CHART_SHA)
            self.assertIn((["git", "push", "origin", "HEAD:main"],), [call.args for call in commands.call_args_list])

    def test_superseded_source_never_changes_gitops_checkout(self):
        response = io.StringIO(json.dumps({"object": {"sha": OTHER_SHA}}))
        with patch.object(gitops, "validate_automatic_release"), \
                patch.dict(os.environ, {"GH_TOKEN": "test-token"}), \
                patch.object(gitops.urllib.request, "urlopen", return_value=response), \
                patch.object(gitops.subprocess, "run") as commands, \
                patch.object(gitops, "release_output") as output, patch("builtins.print"):
            gitops.publish("playground-shell", "staging",
                           [publisher.validate_release("shell", artifact(), source())],
                           source(), None, preserve_chart=True)
            commands.assert_not_called()
            output.assert_not_called()


class RegistryIntegrityTests(unittest.TestCase):
    def test_manifest_body_must_match_digest_header(self):
        registry = Registry(artifact()["repository"])
        with patch.object(registry, "get", return_value=(b"{}", {"Docker-Content-Digest": IMAGE_DIGEST})):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                registry.manifest("main-" + SOURCE_SHA)

    def test_digest_reference_must_match_returned_manifest_not_only_header(self):
        registry = Registry(artifact()["repository"])
        body = b'{"schemaVersion":2}'
        actual_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        with patch.object(registry, "get", return_value=(body, {"Docker-Content-Digest": actual_digest})):
            with self.assertRaises(ValueError):
                registry.manifest(IMAGE_DIGEST)

    def test_branch_label_must_match_caller_and_config_digest_must_match(self):
        for label_branch, tamper in (("dev", False), ("main", False), ("dev", True)):
            registry = Registry(artifact()["repository"])
            body = json.dumps({"config": {"Labels": {
                "org.opencontainers.image.revision": SOURCE_SHA,
                "org.opencontainers.image.source": "https://github.com/" + source()["repository"],
                "io.gitops.source-branch": label_branch}}}).encode()
            config_digest = "sha256:" + hashlib.sha256(body).hexdigest()
            with self.subTest(label_branch=label_branch, tamper=tamper), \
                    patch.object(registry, "manifest", return_value=({"config": {"digest": config_digest}}, IMAGE_DIGEST)), \
                    patch.object(registry, "get", return_value=(body + b" " if tamper else body, {})):
                if label_branch == "dev" and not tamper:
                    self.assertEqual(registry.image(IMAGE_DIGEST, SOURCE_SHA, require_main_label=False, source_branch="dev"),
                                     (IMAGE_DIGEST, source()["repository"]))
                else:
                    with self.assertRaises(ValueError):
                        registry.image(IMAGE_DIGEST, SOURCE_SHA, require_main_label=False, source_branch="dev")


if __name__ == "__main__":
    unittest.main()
