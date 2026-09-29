import base64
import copy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
import urllib.error

PATH = Path(__file__).resolve().parents[2] / "utility-apps/argocd/webhook/files/configure.py"
SPEC = importlib.util.spec_from_file_location("webhook_configure", PATH)
webhook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webhook)


class WebhookSecretTests(unittest.TestCase):
    def setUp(self):
        self.secret = {"metadata": {"resourceVersion": "7"}, "data": {
            "admin.password": "existing-password", "server.secretkey": "existing-signing-key",
            "unrelated-future-key": "retain-this-too"}}
        self.deployment = {"metadata": {"generation": 1}, "spec": {
            "replicas": 1, "template": {"metadata": {"annotations": {}}}},
            "status": {"observedGeneration": 1, "replicas": 1, "updatedReplicas": 1,
                       "readyReplicas": 1, "availableReplicas": 1}}
        self.patches = []

    def request(self, path, patch=None):
        if path == webhook.SECRET:
            if patch:
                self.assertEqual(set(patch["data"]), {webhook.KEY})
                self.assertEqual(patch["metadata"]["resourceVersion"], self.secret["metadata"]["resourceVersion"])
                self.secret["data"].update(patch["data"])
                self.patches.append(path)
            return copy.deepcopy(self.secret)
        self.assertEqual(path, webhook.DEPLOYMENT)
        if patch:
            self.deployment["spec"]["template"]["metadata"]["annotations"].update(
                patch["spec"]["template"]["metadata"]["annotations"])
            self.patches.append(path)
        return copy.deepcopy(self.deployment)

    def test_preserves_existing_credentials_and_repeat_does_not_restart(self):
        original = copy.deepcopy(self.secret["data"])
        value = b"random-test-secret-for-this-test-only"
        webhook.configure(self.request, value)
        self.assertEqual({k: self.secret["data"][k] for k in original}, original)
        self.assertEqual(self.secret["data"][webhook.KEY], base64.b64encode(value).decode())
        self.assertEqual(self.patches, [webhook.SECRET, webhook.DEPLOYMENT])
        webhook.configure(self.request, value)
        self.assertEqual(len(self.patches), 2)

    def test_retries_conflict_and_preserves_concurrent_key(self):
        failed = False

        def race(path, patch=None):
            nonlocal failed
            if path == webhook.SECRET and patch and not failed:
                failed = True
                self.secret["metadata"]["resourceVersion"] = "8"
                self.secret["data"]["new-key"] = "added-concurrently"
                raise urllib.error.HTTPError("kubernetes", 409, "Conflict", {}, None)
            return self.request(path, patch)

        webhook.configure(race, b"another-random-test-secret-only-123", sleep=lambda _: None)
        self.assertEqual(self.secret["data"]["new-key"], "added-concurrently")

    def test_short_secret_and_incomplete_rollout_fail(self):
        request = Mock()
        with self.assertRaises(ValueError):
            webhook.configure(request, b"short")
        request.assert_not_called()
        self.deployment["status"]["updatedReplicas"] = 0
        with self.assertRaises(TimeoutError):
            webhook.configure(self.request, b"another-random-test-secret-only-123",
                              monotonic=Mock(side_effect=[0, 301]))


if __name__ == "__main__":
    unittest.main()
