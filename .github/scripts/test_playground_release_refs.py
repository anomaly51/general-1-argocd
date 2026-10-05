"""Regression coverage for commit-pinned playground promotion."""
from __future__ import annotations

import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import yaml

import promote_playground as promotion

SOURCE_SHA = "a" * 40
OTHER_SHA = "b" * 40
GITOPS_SHA = "c" * 40
IMAGE_DIGEST = "sha256:" + "d" * 64
OTHER_DIGEST = "sha256:" + "e" * 64
SOURCE_REPOSITORY = "anomaly51/playground-shell"
IMAGE_REPOSITORY = "harbor.internal.api-api-api.com/playground/shell"


def profile(tag, environment="staging"):
    return {
        "_release": {
            "repository": "harbor.internal.api-api-api.com/helm-charts",
            "chart": "app", "revision": "0.6.0", "policy": "promote",
            "namespace": "playground-" + environment,
            "sourceRepository": SOURCE_REPOSITORY,
            "sourceBranch": "main", "sourceCommit": SOURCE_SHA,
        },
        "image": {"repository": IMAGE_REPOSITORY, "tag": tag},
        "env": {"CORS_ORIGINS": "https://production.example"},
        "replicaCount": 2,
    }


def release_archive(digest=IMAGE_DIGEST):
    document = {
        "service": "shell", "repository": IMAGE_REPOSITORY,
        "source_repository": SOURCE_REPOSITORY, "source_commit": SOURCE_SHA,
        "source_branch": "main", "digest": digest,
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("release-image.json", json.dumps(document))
    return output.getvalue()


class StagedReferenceTests(unittest.TestCase):
    def test_accepts_legacy_and_full_main_commit_pins(self):
        for tag in ("staging@" + IMAGE_DIGEST, "main-" + SOURCE_SHA + "@" + IMAGE_DIGEST):
            with self.subTest(tag=tag):
                self.assertEqual(promotion.staged_image_digest(tag, SOURCE_SHA), IMAGE_DIGEST)

    def test_rejects_dev_floating_short_or_malformed_pins(self):
        for tag in ("dev-" + SOURCE_SHA + "@" + IMAGE_DIGEST,
                    "main-" + SOURCE_SHA[:12] + "@" + IMAGE_DIGEST,
                    "main-" + SOURCE_SHA, "staging", "latest@" + IMAGE_DIGEST,
                    "main-" + SOURCE_SHA + "@sha256:abcd"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                promotion.staged_image_digest(tag, SOURCE_SHA)

    def test_main_tag_must_match_verified_oci_revision(self):
        with self.assertRaisesRegex(ValueError, "verified main source commit"):
            promotion.staged_image_digest("main-" + OTHER_SHA + "@" + IMAGE_DIGEST, SOURCE_SHA)

    def test_main_tag_requires_full_source_revision(self):
        with self.assertRaises(ValueError):
            promotion.staged_image_digest("main-" + SOURCE_SHA + "@" + IMAGE_DIGEST, "")

    def test_promotion_retains_exact_pin_and_production_settings(self):
        production = profile("prod@" + OTHER_DIGEST, "prod")
        original = copy.deepcopy(production)
        for tag in ("staging@" + IMAGE_DIGEST, "main-" + SOURCE_SHA + "@" + IMAGE_DIGEST):
            with self.subTest(tag=tag):
                staging = profile(tag)
                staging["replicaCount"] = 1
                result = promotion.promoted_values(production, staging, "shell", GITOPS_SHA, SOURCE_SHA)
                self.assertEqual(result["image"]["tag"], tag)
                self.assertEqual(result["replicaCount"], 2)
                self.assertEqual(result["env"], original["env"])
                self.assertEqual(result["_release"]["sourceCommit"], SOURCE_SHA)
                self.assertEqual(result["_release"]["revision"], "0.6.0")
        self.assertEqual(production, original)

    def test_promotion_rechecks_tag_revision_binding(self):
        with self.assertRaises(ValueError):
            promotion.promoted_values(profile("prod@" + OTHER_DIGEST, "prod"),
                                      profile("main-" + OTHER_SHA + "@" + IMAGE_DIGEST),
                                      "shell", GITOPS_SHA, SOURCE_SHA)

    def test_promotion_still_requires_manual_production_policy(self):
        production = profile("prod@" + OTHER_DIGEST, "prod")
        production["_release"]["policy"] = "prod-only"
        with self.assertRaisesRegex(ValueError, "manual promotion"):
            promotion.promoted_values(production, profile("main-" + SOURCE_SHA + "@" + IMAGE_DIGEST),
                                      "shell", GITOPS_SHA, SOURCE_SHA)


class ProvenanceTests(unittest.TestCase):
    def test_oci_labels_still_require_main_and_matching_repository(self):
        for branch, repository in (("main", SOURCE_REPOSITORY),
                                   ("dev", SOURCE_REPOSITORY), ("main", "anomaly51/other")):
            with self.subTest(branch=branch, repository=repository):
                body = json.dumps({"config": {"Labels": {
                    "org.opencontainers.image.revision": SOURCE_SHA,
                    "org.opencontainers.image.source": "https://github.com/" + repository,
                    "io.gitops.source-branch": branch,
                }}}).encode()
                config_digest = "sha256:" + hashlib.sha256(body).hexdigest()
                registry = Mock()
                registry.manifest.return_value = ({"config": {"digest": config_digest}}, IMAGE_DIGEST)
                registry.get.return_value = (body, {})
                if branch == "main" and repository == SOURCE_REPOSITORY:
                    self.assertEqual(promotion.source_revision(registry, IMAGE_DIGEST, SOURCE_REPOSITORY), SOURCE_SHA)
                else:
                    with self.assertRaises(ValueError):
                        promotion.source_revision(registry, IMAGE_DIGEST, SOURCE_REPOSITORY)

    def test_unsuccessful_main_build_is_still_rejected(self):
        with patch.object(promotion, "github_json", return_value={"workflow_runs": []}):
            with self.assertRaisesRegex(ValueError, "successful push-main CI run"):
                promotion.successful_build(SOURCE_REPOSITORY, SOURCE_SHA)

    def test_artifact_must_still_bind_successful_build_to_selected_digest(self):
        artifacts = {"total_count": 1, "artifacts": [{
            "name": "release-image", "expired": False, "id": 12, "size_in_bytes": 512,
        }]}
        for published_digest in (IMAGE_DIGEST, OTHER_DIGEST):
            with self.subTest(digest=published_digest), \
                    patch.object(promotion, "successful_build", return_value=[123]) as builds, \
                    patch.object(promotion, "github_json", return_value=artifacts), \
                    patch.object(promotion, "github_bytes", return_value=release_archive(published_digest)), \
                    patch.object(promotion, "image_manifest", return_value=({}, published_digest)):
                if published_digest == IMAGE_DIGEST:
                    promotion.verify_build_artifact(Mock(), IMAGE_DIGEST, SOURCE_REPOSITORY, SOURCE_SHA, "shell")
                else:
                    with self.assertRaisesRegex(ValueError, "not published"):
                        promotion.verify_build_artifact(Mock(), IMAGE_DIGEST, SOURCE_REPOSITORY, SOURCE_SHA, "shell")
                builds.assert_called_once_with(SOURCE_REPOSITORY, SOURCE_SHA)

    def test_main_rejects_label_tag_mismatch_before_artifact_or_rollout(self):
        staging = profile("main-" + OTHER_SHA + "@" + IMAGE_DIGEST)
        with patch("sys.argv", ["promote_playground.py", "--service", "shell"]), \
                patch.object(promotion, "validate_caller", return_value=SOURCE_REPOSITORY), \
                patch.object(promotion, "git", side_effect=[GITOPS_SHA, yaml.safe_dump(staging)]), \
                patch.object(promotion.subprocess, "run"), patch.object(promotion, "Registry"), \
                patch.object(promotion, "source_revision", return_value=SOURCE_SHA), \
                patch.object(promotion, "verify_build_artifact") as artifact, \
                patch.object(promotion, "wait") as rollout:
            with self.assertRaisesRegex(ValueError, "verified main source commit"):
                promotion.main()
            artifact.assert_not_called()
            rollout.assert_not_called()

    def test_main_keeps_artifact_and_healthy_rollout_checks_for_both_pin_formats(self):
        for tag in ("staging@" + IMAGE_DIGEST, "main-" + SOURCE_SHA + "@" + IMAGE_DIGEST):
            with self.subTest(tag=tag), tempfile.TemporaryDirectory(prefix="playground-promote-test-") as temporary:
                root = Path(temporary)
                path = root / "playground-shell/values/prod.yaml"
                path.parent.mkdir(parents=True)
                path.write_text(yaml.safe_dump(profile("prod@" + OTHER_DIGEST, "prod")))
                staging = profile(tag)
                with patch("sys.argv", ["promote_playground.py", "--service", "shell"]), \
                        patch.object(promotion, "validate_caller", return_value=SOURCE_REPOSITORY), \
                        patch.object(promotion, "git", side_effect=[GITOPS_SHA, yaml.safe_dump(staging)]), \
                        patch.object(promotion.subprocess, "run"), patch.object(promotion, "Registry") as registry, \
                        patch.object(promotion, "source_revision", return_value=SOURCE_SHA) as source, \
                        patch.object(promotion, "verify_build_artifact") as artifact, \
                        patch.object(promotion, "wait") as rollout, \
                        patch.object(promotion, "validate_chart") as chart, \
                        patch.object(promotion, "Path", return_value=root), patch("builtins.print"):
                    promotion.main()
                    source.assert_called_once_with(registry.return_value, IMAGE_DIGEST, SOURCE_REPOSITORY)
                    artifact.assert_called_once_with(registry.return_value, IMAGE_DIGEST,
                                                     SOURCE_REPOSITORY, SOURCE_SHA, "shell")
                    rollout.assert_called_once()
                    self.assertEqual(rollout.call_args.args[:2], ("playground-shell", "staging"))
                    chart.assert_called_once()
                    self.assertEqual(yaml.safe_load(path.read_text())["image"]["tag"], tag)



if __name__ == "__main__":
    unittest.main()
