"""Offline safety checks for retirement; no Harbor/Vault/Kubernetes access."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


MODULE = Path(__file__).resolve().parents[1] / "files" / "configure.py"
SPEC = importlib.util.spec_from_file_location("configure", MODULE)
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class FakeHarbor:
    def __init__(self, policies=(), retain=False):
        self.policies = deepcopy(list(policies))
        self.calls = []
        self.retain = retain

    def __call__(self, method, path, payload=None):
        self.calls.append((method, path))
        if method == "GET":
            return deepcopy(self.policies)
        if method == "DELETE":
            if not self.retain:
                self.policies = [p for p in self.policies
                                 if path != f"{configure.POLICIES}/{p['id']}"]
            return None
        raise AssertionError("Unexpected mutation")


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.config = {"state": "absent", "expectedId": 1, "enabled": False,
                       "endpoint": configure.ENDPOINT}
        self.policy = {"id": 1, **configure.desired_policy(self.config, "x" * 32)}

    def test_deletes_only_owned_policy_and_verifies(self):
        other = {"id": 2, "name": "unrelated"}
        api = FakeHarbor([self.policy, other])
        self.assertEqual(configure.configure(api, self.config, ""), "deleted")
        self.assertEqual(api.policies, [other])
        self.assertEqual([c for c in api.calls if c[0] == "DELETE"],
                         [("DELETE", configure.POLICIES + "/1")])
        self.assertEqual(api.calls[-1][0], "GET")

    def test_absent_is_idempotent_and_needs_no_webhook_token(self):
        api = FakeHarbor()
        self.assertEqual(configure.configure(api, self.config, ""), "absent")
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_wrong_id_description_event_or_endpoint_never_mutates(self):
        variants = [{"id": 2}, {"description": "someone else"},
                    {"event_types": ["DELETE_ARTIFACT"]},
                    {"targets": [{"type": "http", "address": "https://elsewhere"}]},
                    {"targets": []}, {"targets": [None]}]
        for replacement in variants:
            with self.subTest(replacement=replacement):
                api = FakeHarbor([{**self.policy, **replacement}])
                with self.assertRaises(ValueError):
                    configure.configure(api, self.config, "")
                self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_duplicate_names_never_mutate(self):
        api = FakeHarbor([self.policy, {**self.policy, "id": 2}])
        with self.assertRaises(ValueError):
            configure.configure(api, self.config, "")
        self.assertTrue(all(method == "GET" for method, _ in api.calls))

    def test_retained_policy_fails_verification(self):
        with self.assertRaises(ValueError):
            configure.configure(FakeHarbor([self.policy], retain=True), self.config, "")

    def test_invalid_config_never_contacts_harbor(self):
        for replacement in ({"state": "delete-everything"}, {"expectedId": True},
                            {"expectedId": 0}, {"enabled": "false"},
                            {"endpoint": "https://elsewhere"}):
            with self.subTest(replacement=replacement):
                api = FakeHarbor([self.policy])
                with self.assertRaises(ValueError):
                    configure.configure(api, {**self.config, **replacement}, "")
                self.assertEqual(api.calls, [])

    def test_delete_collection_and_payload_rejected_before_network(self):
        request = configure.api_client("unused", "unused")
        for path, payload in ((configure.POLICIES, None),
                              (configure.POLICIES + "?page=1", None),
                              (configure.POLICIES + "/1", {})):
            with self.assertRaises(ValueError):
                request("DELETE", path, payload)


if __name__ == "__main__":
    unittest.main()
