import copy
import hashlib
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).with_name("release_marker.py")
if not MODULE_PATH.exists():
    MODULE_PATH = Path(__file__).with_name("cutline-studio-sync.py")
spec = importlib.util.spec_from_file_location("release_marker", MODULE_PATH)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def marker(sequence=2, digit="b"):
    sha = digit * 40
    tag = "sha-" + sha[:12]
    return {
        "schema": 1, "source_repository": release.SOURCE_REPOSITORY, "channel": "main",
        "source_sha": sha, "source_run_number": sequence, "source_run_id": sequence * 100,
        "tag": tag, "images": {name: {"repository": repo, "tag": tag, "digest": "sha256:" + digit * 64} for name, repo in release.REPOSITORIES.items()},
    }


def values(tag="bootstrap-pending"):
    return "# keep me\nimages:\n" + "".join(f"  {name}:\n    repository: {repo}\n    tag: '{tag}' # keep comment\n" for name, repo in release.REPOSITORIES.items()) + "storage:\n  size: 80Gi\n"


class FakeRegistry:
    def __init__(self, candidate=None, previous=None):
        self.candidate = candidate or marker()
        self.previous = previous
        self.immutable = copy.deepcopy(self.candidate)
        self.digests = {name: image["digest"] for name, image in self.candidate["images"].items()}

    def marker(self, reference, missing_ok=False):
        if reference == "main":
            return self.candidate
        if reference == self.candidate["tag"]:
            return self.immutable
        return self.previous

    def manifest(self, repository, reference):
        return {}, self.digests[repository.split("/")[-1]]


class ReleaseTests(unittest.TestCase):
    def test_real_scratch_null_and_empty_array_layers_are_equivalent(self):
        # Reproduced with an actual BuildKit FROM scratch + LABEL OCI export:
        # manifest.layers=null; config.rootfs={type:layers,diff_ids:null}.
        for layers in ([], None):
            for diff_ids in ([], None):
                payload = marker()
                config = {"rootfs": {"type": "layers", "diff_ids": diff_ids},
                          "config": {"Labels": {release.LABEL: json.dumps(payload)}}}
                body = json.dumps(config).encode()
                digest = "sha256:" + hashlib.sha256(body).hexdigest()
                manifest = {"config": {"digest": digest}, "layers": layers}
                registry = release.Registry("dummy", "dummy")
                with patch.object(registry, "manifest", return_value=(manifest, "unused")), patch.object(registry, "get", return_value=(body, {})):
                    self.assertEqual(registry.marker("main"), payload)
                    self.assertEqual(registry.marker(payload["tag"]), payload)

    def test_nonempty_missing_or_malformed_layers_fail_closed(self):
        payload = marker()
        registry = release.Registry("dummy", "dummy")
        for layers in ([{"digest": "sha256:" + "a" * 64}], {}, "", False, "missing"):
            manifest = {"config": {"digest": "sha256:" + "b" * 64}, "layers": layers}
            if layers == "missing":
                del manifest["layers"]
            with patch.object(registry, "manifest", return_value=(manifest, "unused")), patch.object(registry, "get") as get:
                with self.assertRaises(release.RegistryError):
                    registry.marker("main")
                get.assert_not_called()
        for rootfs in ({}, {"type": "other", "diff_ids": []}, {"type": "layers"},
                       {"type": "layers", "diff_ids": ["sha256:" + "a" * 64]}):
            config = {"rootfs": rootfs, "config": {"Labels": {release.LABEL: json.dumps(payload)}}}
            body = json.dumps(config).encode()
            digest = "sha256:" + hashlib.sha256(body).hexdigest()
            manifest = {"config": {"digest": digest}, "layers": None}
            with patch.object(registry, "manifest", return_value=(manifest, "unused")), patch.object(registry, "get", return_value=(body, {})):
                with self.assertRaises(release.RegistryError):
                    registry.marker("main")

    def test_schema_requires_fixed_origin_full_sha_and_both_immutable_images(self):
        release.validate_marker(marker())
        alterations = [("source_repository", "other/repo"), ("channel", "dev"), ("source_sha", "b" * 39), ("tag", "latest"), ("source_run_number", True), ("source_run_id", 0), ("schema", 2)]
        for key, value in alterations:
            bad = marker(); bad[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.validate_marker(bad)
        for field, value in [("repository", "external.example/api"), ("tag", "main"), ("digest", "sha256:bad")]:
            bad = marker(); bad["images"]["api"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                release.validate_marker(bad)
        for bad in [dict(marker(), extra=True), dict(marker(), images={"api": marker()["images"]["api"]})]:
            with self.assertRaises(ValueError):
                release.validate_marker(bad)

    def test_monotonic_release_accepts_new_rejects_stale_and_ambiguous(self):
        old, new = marker(1, "a"), marker(2, "b")
        self.assertTrue(release.permitted_release(new, old, old["tag"]))
        self.assertFalse(release.permitted_release(new, None, new["tag"]))
        self.assertTrue(release.permitted_release(new, None, "bootstrap-pending"))
        for candidate, previous, current in [(old, new, new["tag"]), (new, None, old["tag"]), (new, old, "latest"), (new, marker(2, "a"), old["tag"])]:
            with self.assertRaises(ValueError):
                release.permitted_release(candidate, previous, current)

    def test_sync_changes_exactly_two_scalars_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="cutline-release-tests-") as directory:
            path = Path(directory, "values.yaml")
            original = values()
            path.write_text(original)
            release.sync(FakeRegistry(), path)
            self.assertEqual(path.read_text(), original.replace("bootstrap-pending", marker()["tag"]))
            first = path.read_bytes()
            release.sync(FakeRegistry(), path)
            self.assertEqual(path.read_bytes(), first)

    def test_failed_digest_immutable_marker_and_mixed_tags_preserve_file(self):
        bad_digest = FakeRegistry(); bad_digest.digests["api"] = "sha256:" + "f" * 64
        bad_immutable = FakeRegistry(); bad_immutable.immutable["source_run_id"] = 900
        for registry, original in [(bad_digest, values()), (bad_immutable, values()), (FakeRegistry(), values().replace("bootstrap-pending", "sha-aaaaaaaaaaaa", 1))]:
            with tempfile.TemporaryDirectory(prefix="cutline-release-tests-") as directory:
                path = Path(directory, "values.yaml"); path.write_text(original)
                with self.assertRaises(ValueError):
                    release.sync(registry, path)
                self.assertEqual(path.read_text(), original)

    def test_dry_run_and_no_marker_never_change_file(self):
        with tempfile.TemporaryDirectory(prefix="cutline-release-tests-") as directory:
            path = Path(directory, "values.yaml"); path.write_text(values())
            release.sync(FakeRegistry(), path, dry_run=True)
            self.assertEqual(path.read_text(), values())
            fake = FakeRegistry(); fake.candidate = None
            release.sync(fake, path)
            self.assertEqual(path.read_text(), values())

    def test_source_checkout_must_still_be_current_sha_before_any_publication(self):
        env = {"GITHUB_SHA": "a" * 40, "GITHUB_REPOSITORY": release.SOURCE_REPOSITORY, "GITHUB_REF": "refs/heads/main"}
        with patch.dict(os.environ, env), patch.object(release.subprocess, "check_output", return_value="b" * 40), patch.object(release.subprocess, "run") as run, patch.object(release, "output") as output:
            release.publish(FakeRegistry())
            run.assert_not_called()
            output.assert_called_once_with("published", "false")

    def test_untrusted_registry_auth_realm_and_redirect_rejected(self):
        registry = release.Registry("dummy", "dummy")
        with self.assertRaises(release.RegistryError):
            registry.token("cutline-studio/api", 'Bearer realm="https://evil.invalid/token",service="harbor-registry"')
        with self.assertRaises(release.RegistryError):
            registry.request("http://" + release.REGISTRY + "/v2/")
        with self.assertRaises(release.RegistryError):
            registry.get("applications/other", "manifests/main")
        with self.assertRaises(release.RegistryError):
            release.NoRedirect().redirect_request(None, None, 302, "redirect", {}, "https://evil.invalid")

    def test_marker_schema_and_updater_reject_duplicate_or_missing_yaml_fields(self):
        for text in [values() + "images:\n", values().replace("  api:\n", "  api:\n  api:\n"), values().replace("    tag: 'bootstrap-pending' # keep comment\n", "", 1), values().replace("cutline-studio/api", "applications/other")]:
            with self.assertRaises(ValueError):
                release.update_tags(text, marker()["tag"])
        crlf = values().replace("\n", "\r\n")
        self.assertEqual(release.update_tags(crlf, marker()["tag"]), crlf.replace("bootstrap-pending", marker()["tag"]))


if __name__ == "__main__":
    unittest.main()
