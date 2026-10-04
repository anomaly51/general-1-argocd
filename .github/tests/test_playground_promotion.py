import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import promote_playground as promote


class PlaygroundPromotion(unittest.TestCase):
    def setUp(self):
        self.prod = {"_release": {"policy": "promote", "namespace": "playground-prod", "revision": "a" * 40},
                     "image": {"repository": "harbor.internal.api-api-api.com/playground/shell", "tag": "prod@sha256:" + "b" * 64},
                     "resources": {"requests": {"memory": "64Mi"}}, "replicaCount": 1,
                     "env": {"URL": "https://production.example"}}
        self.stage = copy.deepcopy(self.prod)
        self.stage["_release"].update(namespace="playground-staging", revision="c" * 40)
        self.stage["image"]["tag"] = "staging@sha256:" + "d" * 64
        self.stage["env"]["URL"] = "https://staging.example"

    def promote(self):
        return promote.promoted_values(self.prod, self.stage, "shell", "e" * 40, "f" * 40)

    def test_only_release_changes_and_production_configuration_is_preserved(self):
        result = self.promote()
        self.assertEqual(result["image"]["tag"], self.stage["image"]["tag"])
        for key in ("resources", "replicaCount", "env"):
            self.assertEqual(result[key], self.prod[key])
        self.assertEqual(result["_release"]["namespace"], "playground-prod")
        self.assertEqual(result["_release"]["sourceBranch"], "main")
        self.assertEqual(result["_release"]["imageKeys"], ["image"])
        self.assertTrue(self.prod["image"]["tag"].startswith("prod@"))

    def test_mutable_tag_foreign_repository_and_wrong_namespace_are_rejected(self):
        for key, value in (("tag", "staging"), ("repository", "ghcr.io/foreign/image")):
            original = self.stage["image"][key]
            self.stage["image"][key] = value
            with self.assertRaises(ValueError):
                self.promote()
            self.stage["image"][key] = original
        self.stage["_release"]["namespace"] = "playground-prod"
        with self.assertRaises(ValueError):
            self.promote()

    def test_only_manual_main_from_matching_repository_is_accepted(self):
        claims = {"GITHUB_REPOSITORY": "anomaly51/playground-shell", "GITHUB_REF": "refs/heads/main",
                  "GITHUB_EVENT_NAME": "workflow_dispatch"}
        with patch.dict(os.environ, claims):
            self.assertEqual(promote.validate_caller("shell"), claims["GITHUB_REPOSITORY"])
            with self.assertRaises(ValueError):
                promote.validate_caller("event-hub")
            for key, value in (("GITHUB_REF", "refs/heads/dev"), ("GITHUB_EVENT_NAME", "push")):
                with patch.dict(os.environ, {key: value}), self.assertRaises(ValueError):
                    promote.validate_caller("shell")

    def test_invalid_chart_and_automatic_production_policy_are_rejected(self):
        self.stage["_release"]["revision"] = "main"
        with self.assertRaises(ValueError):
            self.promote()
        self.stage["_release"]["revision"] = "c" * 40
        self.prod["_release"]["policy"] = "prod-only"
        with self.assertRaises(ValueError):
            self.promote()


class ReleaseProvenance(unittest.TestCase):
    def setUp(self):
        self.repository = "anomaly51/playground-shell"
        self.revision = "a" * 40
        self.digest = "sha256:" + "b" * 64
        self.child = "sha256:" + "c" * 64
        self.config = json.dumps({"config": {"Labels": {
            "org.opencontainers.image.revision": self.revision,
            "org.opencontainers.image.source": "https://github.com/" + self.repository,
            "io.gitops.source-branch": "main",
        }}}).encode()
        self.manifest = {"config": {"digest": "sha256:" + hashlib.sha256(self.config).hexdigest()}}
        self.index = {"manifests": [{"platform": {"os": "linux", "architecture": "amd64"},
                                     "digest": self.child}]}
        self.registry = Mock()
        self.registry.manifest.side_effect = lambda digest: (
            (self.index, self.digest) if digest == self.digest else (self.manifest, self.child))
        self.registry.get.return_value = (self.config, {})
        self.document = {"service": "shell", "repository": "harbor.internal.api-api-api.com/playground/shell",
                         "source_repository": self.repository, "source_commit": self.revision,
                         "source_branch": "main", "digest": self.digest}
        self.run = {"id": 123, "head_sha": self.revision, "head_branch": "main", "event": "push",
                    "status": "completed", "conclusion": "success", "path": ".github/workflows/ci.yaml",
                    "repository": {"full_name": self.repository},
                    "head_repository": {"full_name": self.repository}}
        self.artifact = {"id": 456, "name": "release-image", "expired": False, "size_in_bytes": 500}

    def archive(self, document=None, *, filename="release-image.json", raw=None):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(filename, raw if raw is not None else json.dumps(document or self.document))
        return buffer.getvalue()

    def verify(self, *, digest=None, document=None, artifacts=None):
        listing = [self.artifact] if artifacts is None else artifacts
        with patch.object(promote, "github_json", side_effect=[{"workflow_runs": [self.run]},
                {"artifacts": listing, "total_count": len(listing)}]), patch.object(
                promote, "github_bytes", return_value=self.archive(document)):
            promote.verify_build_artifact(self.registry, digest or self.digest,
                                          self.repository, self.revision, "shell")

    def test_exact_main_build_index_and_verified_amd64_child_are_accepted(self):
        self.assertEqual(promote.source_revision(self.registry, self.digest, self.repository), self.revision)
        self.verify()
        self.verify(digest=self.child)

    def test_tampered_root_or_child_manifest_is_rejected(self):
        self.registry.manifest.return_value = (self.index, self.child)
        self.registry.manifest.side_effect = None
        with self.assertRaisesRegex(ValueError, "Staging manifest digest"):
            promote.source_revision(self.registry, self.digest, self.repository)
        self.registry.manifest.side_effect = [(self.index, self.digest), (self.manifest, self.digest)]
        with self.assertRaisesRegex(ValueError, "platform manifest digest"):
            promote.source_revision(self.registry, self.digest, self.repository)

    def test_configuration_digest_and_source_repository_are_verified(self):
        self.registry.get.return_value = (b"tampered", {})
        with self.assertRaisesRegex(ValueError, "configuration digest"):
            promote.source_revision(self.registry, self.digest, self.repository)
        self.registry.get.return_value = (self.config, {})
        with self.assertRaisesRegex(ValueError, "repository.*main CI"):
            promote.source_revision(self.registry, self.digest, "anomaly51/playground-event-hub")

    def test_artifact_identity_fields_must_match_exactly(self):
        for key in ("service", "repository", "source_repository", "source_commit", "source_branch"):
            with self.subTest(field=key):
                document = dict(self.document, **{key: "foreign"})
                with self.assertRaisesRegex(ValueError, "does not belong"):
                    self.verify(document=document)

    def test_unpublished_staging_digest_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "was not published"):
            self.verify(digest="sha256:" + "d" * 64)
        with self.assertRaisesRegex(ValueError, "Invalid image digest"):
            self.verify(document=dict(self.document, digest="staging"))

    def test_successful_but_skipped_publish_has_no_artifact_and_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "no release-image artifact"):
            self.verify(artifacts=[])

    def test_non_successful_or_non_main_ci_is_rejected(self):
        for key, value in (("conclusion", "skipped"), ("conclusion", "failure"),
                           ("status", "in_progress"), ("head_branch", "dev"),
                           ("event", "workflow_dispatch"), ("head_sha", "d" * 40),
                           ("path", ".github/workflows/foreign.yaml")):
            with self.subTest(field=key, value=value), patch.object(promote, "github_json", return_value={
                    "workflow_runs": [dict(self.run, **{key: value})]}):
                with self.assertRaisesRegex(ValueError, "no successful push-main"):
                    promote.successful_build(self.repository, self.revision)

    def test_foreign_source_run_is_rejected(self):
        for key in ("repository", "head_repository"):
            with self.subTest(field=key), patch.object(promote, "github_json", return_value={
                    "workflow_runs": [dict(self.run, **{key: {"full_name": "foreign/repository"}})]}):
                with self.assertRaises(ValueError):
                    promote.successful_build(self.repository, self.revision)

    def test_expired_duplicate_and_oversized_artifacts_are_rejected(self):
        for artifacts in ([dict(self.artifact, expired=True)], [self.artifact, self.artifact],
                          [dict(self.artifact, size_in_bytes=promote.MAX_RESPONSE_BYTES + 1)]):
            with self.subTest(artifacts=artifacts), self.assertRaises(ValueError):
                self.verify(artifacts=artifacts)

    def test_archive_path_zip_bomb_unknown_fields_and_size_limits(self):
        bodies = [self.archive(filename="../release-image.json"),
                  self.archive(raw=b"A" * (promote.MAX_RELEASE_BYTES + 1)),
                  self.archive(document=dict(self.document, extra="untrusted")),
                  b"A" * (promote.MAX_RESPONSE_BYTES + 1), b"not a zip archive"]
        for body in bodies:
            with self.subTest(size=len(body)), self.assertRaises(ValueError):
                promote.release_document(body)


class ArtifactTransport(unittest.TestCase):
    def response(self, body, headers=None):
        response = io.BytesIO(body)
        response.headers = headers or {}
        return response

    @patch.dict(os.environ, {"GH_TOKEN": "never-forward-this-token"})
    def test_storage_redirect_drops_authorization(self):
        location = "https://productionresults.blob.core.windows.net/artifact?sig=private-signature"
        redirect = urllib.error.HTTPError("https://api.github.com/artifact", 302, "Found",
                                          {"Location": location}, io.BytesIO())
        opener = Mock()
        opener.open.side_effect = [redirect, self.response(b"archive")]
        with patch.object(promote.urllib.request, "build_opener", return_value=opener):
            self.assertEqual(promote.github_bytes("/repos/owner/repo/actions/artifacts/1/zip", archive=True),
                             b"archive")
        first, second = [call.args[0] for call in opener.open.call_args_list]
        self.assertEqual(first.get_header("Authorization"), "Bearer never-forward-this-token")
        self.assertIsNone(second.get_header("Authorization"))
        self.assertEqual(second.full_url, location)

    @patch.dict(os.environ, {"GH_TOKEN": "never-forward-this-token"})
    def test_unsafe_redirect_errors_do_not_disclose_signed_url(self):
        opener = Mock()
        opener.open.side_effect = urllib.error.HTTPError("https://api.github.com/artifact", 302, "Found",
            {"Location": "https://evil.invalid/artifact?sig=private-signature"}, io.BytesIO())
        with patch.object(promote.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "Unexpected GitHub artifact") as caught:
                promote.github_bytes("/repos/owner/repo/actions/artifacts/1/zip", archive=True)
        self.assertNotIn("private-signature", str(caught.exception))
        self.assertNotIn("never-forward", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)

    @patch.dict(os.environ, {"GH_TOKEN": "test-token"})
    def test_response_size_limit_applies_without_content_length(self):
        opener = Mock()
        opener.open.return_value = self.response(b"A" * (promote.MAX_RESPONSE_BYTES + 1))
        with patch.object(promote.urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "size limit"):
                promote.github_bytes("/repos/owner/repo/actions/artifacts/1/zip", archive=True)


if __name__ == "__main__":
    unittest.main()
