import contextlib
import copy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch
import urllib.error

import yaml


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "utility-apps/harbor/playground-webhook"
SPEC = importlib.util.spec_from_file_location("harbor_playground_webhook", CHART / "files/configure.py")
webhook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(webhook)
TOKEN = "test-only-random-secret-at-least-32-bytes"
CONFIG = {"enabled": True, "endpoint": webhook.ENDPOINT}


class FakeHarbor:
    def __init__(self, policies=()):
        self.policies = copy.deepcopy(list(policies))
        self.writes = []

    def request(self, method, path, payload=None):
        if method == "GET":
            self.assert_list_path(path)
            page = int(path.split("?page=")[1].split("&")[0])
            return copy.deepcopy(self.policies[(page - 1) * 100:page * 100])
        self.writes.append((method, path, copy.deepcopy(payload)))
        if method == "POST":
            assert path == webhook.POLICIES
            self.policies.append(dict(copy.deepcopy(payload), id=5001))
        elif method == "PUT":
            ident = int(path.rsplit("/", 1)[1])
            index = next(i for i, policy in enumerate(self.policies) if policy["id"] == ident)
            self.policies[index] = dict(copy.deepcopy(payload), id=ident)
        else:
            raise AssertionError("Unexpected mutation")

    @staticmethod
    def assert_list_path(path):
        assert path.startswith(webhook.POLICIES + "?page=")
        assert path.endswith("&page_size=100")


class HarborPolicyTests(unittest.TestCase):
    def desired(self, ident=7):
        return dict(webhook.desired_policy(CONFIG, TOKEN), id=ident)

    def test_creates_one_policy_and_repeated_run_does_not_write(self):
        harbor = FakeHarbor()
        self.assertEqual(webhook.configure(harbor.request, CONFIG, TOKEN), "created")
        self.assertEqual(webhook.configure(harbor.request, CONFIG, TOKEN), "unchanged")
        self.assertEqual(len(harbor.policies), 1)
        self.assertEqual(len(harbor.writes), 1)
        self.assertEqual(harbor.writes[0][:2], ("POST", webhook.POLICIES))

    def test_updates_only_named_policy_and_preserves_unrelated_policies(self):
        unrelated = {"id": 8, "name": "other-system", "targets": [{"address": "https://example.com"}]}
        ours = self.desired()
        ours["enabled"] = False
        harbor = FakeHarbor([unrelated, ours])
        self.assertEqual(webhook.configure(harbor.request, CONFIG, TOKEN), "updated")
        self.assertEqual(harbor.policies[0], unrelated)
        self.assertEqual(harbor.writes[0][:2], ("PUT", webhook.POLICIES + "/7"))

    def test_token_rotation_updates_existing_policy_without_duplicate(self):
        harbor = FakeHarbor([self.desired()])
        token = TOKEN + "-rotated"
        self.assertEqual(webhook.configure(harbor.request, CONFIG, token), "updated")
        self.assertEqual(harbor.policies[0]["targets"][0]["auth_header"], token)
        self.assertEqual(len(harbor.policies), 1)

    def test_omitted_false_cert_verify_matches_after_create_and_on_repeat(self):
        harbor = FakeHarbor()

        def request(method, path, payload=None):
            response = harbor.request(method, path, payload)
            if method == "GET":
                for policy in response:
                    for target in policy.get("targets", []):
                        if target.get("skip_cert_verify") is False:
                            del target["skip_cert_verify"]
            return response

        self.assertEqual(webhook.configure(request, CONFIG, TOKEN), "created")
        self.assertEqual(webhook.configure(request, CONFIG, TOKEN), "unchanged")
        self.assertEqual(len(harbor.writes), 1)
        self.assertIs(harbor.writes[0][2]["targets"][0]["skip_cert_verify"], False)

    def test_explicit_insecure_or_null_cert_verify_is_still_drift(self):
        for value in (True, None):
            with self.subTest(value=value):
                policy = self.desired()
                policy["targets"][0]["skip_cert_verify"] = value
                harbor = FakeHarbor([policy])
                self.assertEqual(webhook.configure(harbor.request, CONFIG, TOKEN), "updated")
                self.assertIs(harbor.policies[0]["targets"][0]["skip_cert_verify"], False)
                self.assertIs(policy["targets"][0]["skip_cert_verify"], value)

    def test_can_disable_only_the_owned_policy_without_deleting(self):
        harbor = FakeHarbor([self.desired()])
        webhook.configure(harbor.request, dict(CONFIG, enabled=False), TOKEN)
        self.assertFalse(harbor.policies[0]["enabled"])
        self.assertEqual([item[0] for item in harbor.writes], ["PUT"])

    def test_scans_all_pages_before_deciding_to_create(self):
        others = [{"id": i + 100, "name": "unrelated-" + str(i)} for i in range(100)]
        harbor = FakeHarbor(others + [self.desired()])
        self.assertEqual(webhook.configure(harbor.request, CONFIG, TOKEN), "unchanged")
        self.assertFalse(harbor.writes)

    def test_duplicate_named_policies_fail_closed_even_across_pages(self):
        others = [{"id": i + 100, "name": "unrelated-" + str(i)} for i in range(99)]
        harbor = FakeHarbor([self.desired()] + others + [self.desired(9)])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            webhook.configure(harbor.request, CONFIG, TOKEN)
        self.assertFalse(harbor.writes)

    def test_unexpected_list_or_invalid_identity_cannot_trigger_writes(self):
        for body in ({}, ["not-a-policy"], [{"name": webhook.NAME, "id": "7"}],
                     [{"name": webhook.NAME, "id": True}]):
            with self.subTest(body=body):
                request = Mock(return_value=body)
                with self.assertRaises(ValueError):
                    webhook.configure(request, CONFIG, TOKEN)
                self.assertTrue(all(call.args[0] == "GET" for call in request.call_args_list))

    def test_verifies_persisted_policy_and_fails_if_harbor_drops_a_field(self):
        request = Mock(side_effect=[[], None, [dict(self.desired(), enabled=False)]])
        with self.assertRaisesRegex(ValueError, "did not retain"):
            webhook.configure(request, CONFIG, TOKEN)

    def test_only_private_https_endpoint_and_safe_token_are_accepted(self):
        for endpoint in ("http://example.com", "https://example.com", webhook.ENDPOINT + "?type=harbor",
                         webhook.ENDPOINT + "#fragment", "https://user:pass@example.com/webhook"):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                webhook.desired_policy(dict(CONFIG, endpoint=endpoint), TOKEN)
        for token in ("short", TOKEN + "\n", TOKEN + "\x00", TOKEN + "é"):
            with self.subTest(token=repr(token)), self.assertRaises(ValueError):
                webhook.desired_policy(CONFIG, token)
        for config in (dict(CONFIG, enabled="true"), dict(CONFIG, project="other")):
            with self.assertRaises(ValueError):
                webhook.desired_policy(config, TOKEN)

    def test_policy_has_only_push_default_json_with_certificate_verification(self):
        policy = webhook.desired_policy(CONFIG, TOKEN)
        self.assertEqual(policy["event_types"], ["PUSH_ARTIFACT"])
        self.assertEqual(policy["targets"], [{"type": "http", "address": webhook.ENDPOINT,
                                            "auth_header": TOKEN, "skip_cert_verify": False,
                                            "payload_format": "Default"}])

    def test_api_client_refuses_deletes_other_projects_and_redirects(self):
        with patch.object(webhook.urllib.request, "build_opener") as build:
            request = webhook.api_client("robot$playground", "test-only-password")
            for method, path in (("DELETE", webhook.POLICIES + "/7"),
                                 ("GET", "/projects/other/webhook/policies"),
                                 ("PUT", webhook.POLICIES + "/7/extra")):
                with self.subTest(method=method, path=path), self.assertRaises(ValueError):
                    request(method, path)
            build.return_value.open.assert_not_called()
            self.assertTrue(any(isinstance(handler, webhook.NoRedirect) for handler in build.call_args.args))
        self.assertIsNone(webhook.NoRedirect().redirect_request(None, None, 302, None, None,
                                                               "https://outside.example"))

    def test_api_uses_verified_tls_and_raw_request_bodies_are_not_logged(self):
        response = Mock()
        response.read.return_value = b"[]"
        with patch.object(webhook.ssl, "create_default_context") as tls, \
                patch.object(webhook.urllib.request, "build_opener") as build:
            build.return_value.open.return_value.__enter__.return_value = response
            request = webhook.api_client("robot$playground", "test-only-password")
            request("GET", webhook.POLICIES + "?page=1&page_size=100")
            tls.assert_called_once_with()
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, webhook.API + webhook.POLICIES + "?page=1&page_size=100")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 20)
        error = urllib.error.HTTPError("https://example.com", 403, TOKEN, {}, io.BytesIO(TOKEN.encode()))
        self.addCleanup(error.close)
        with patch.object(webhook.Path, "read_text", return_value=json.dumps(CONFIG)), \
                patch.object(webhook, "api_client", side_effect=error), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as caught:
                webhook.main()
        self.assertEqual(str(caught.exception), "Harbor policy reconciliation failed: HTTP 403")
        self.assertNotIn(TOKEN, output.getvalue())


class HarborWebhookChartTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rendered = subprocess.check_output(["helm", "template", "playground-webhook", str(CHART),
                                            "--namespace", "harbor"], text=True)
        cls.documents = [doc for doc in yaml.safe_load_all(rendered) if doc]

    def document(self, kind, name):
        return next(doc for doc in self.documents if doc["kind"] == kind and doc["metadata"]["name"] == name)

    def test_vault_separates_robot_credentials_from_webhook_token(self):
        auth = self.document("VaultAuth", "harbor-playground-webhook")["spec"]
        self.assertEqual(auth["kubernetes"], {"role": "harbor-playground-webhook",
                         "serviceAccount": "harbor-playground-webhook", "audiences": ["vault"],
                         "tokenExpirationSeconds": 600})
        secrets = [doc for doc in self.documents if doc["kind"] == "VaultStaticSecret"]
        self.assertEqual(len(secrets), 2)
        fields = set()
        for secret in secrets:
            spec = secret["spec"]
            self.assertEqual(spec["vaultAuthRef"], "harbor-playground-webhook")
            expected = ({"username", "password"}
                        if secret["metadata"]["name"] == "harbor-playground-webhook-api" else {"secret"})
            self.assertEqual(spec["path"], "ci/harbor-playground-webhook"
                             if expected == {"username", "password"} else "ci/playground-image-updater-webhook")
            transform = spec["destination"]["transformation"]
            self.assertTrue(transform["excludeRaw"])
            self.assertEqual(transform["excludes"], [".*"])
            self.assertEqual(set(transform["templates"]), expected)
            for key, template in transform["templates"].items():
                fields.add(key)
                self.assertEqual(template["text"], '{{ get .Secrets "' + key + '" }}')
        self.assertEqual(fields, {"username", "password", "secret"})

    def test_job_has_no_kubernetes_api_rights_or_public_listener(self):
        forbidden = {"Secret", "Service", "Ingress", "HTTPRoute", "Role", "RoleBinding",
                     "ClusterRole", "ClusterRoleBinding"}
        self.assertFalse(forbidden & {doc["kind"] for doc in self.documents})
        sa = self.document("ServiceAccount", "harbor-playground-webhook")
        self.assertFalse(sa["automountServiceAccountToken"])
        job = self.document("Job", "harbor-playground-webhook-configure")
        self.assertEqual(job["metadata"]["annotations"]["argocd.argoproj.io/hook"], "Sync")
        self.assertEqual(job["metadata"]["annotations"]["argocd.argoproj.io/hook-delete-policy"], "BeforeHookCreation")
        self.assertEqual(job["spec"]["activeDeadlineSeconds"], 300)
        pod = job["spec"]["template"]["spec"]
        self.assertFalse(pod["automountServiceAccountToken"])
        container = pod["containers"][0]
        self.assertTrue(container["securityContext"]["readOnlyRootFilesystem"])
        self.assertFalse(container["securityContext"]["allowPrivilegeEscalation"])
        self.assertEqual(container["securityContext"]["capabilities"], {"drop": ["ALL"]})
        self.assertTrue(all(mount["readOnly"] for mount in container["volumeMounts"]))

    def test_rendered_policy_matches_receiver_contract_without_query_string(self):
        config = self.document("ConfigMap", "harbor-playground-webhook-configure")["data"]
        self.assertEqual(json.loads(config["policy.json"]), CONFIG)
        self.assertEqual(config["configure.py"], (CHART / "files/configure.py").read_text())


if __name__ == "__main__":
    unittest.main()
