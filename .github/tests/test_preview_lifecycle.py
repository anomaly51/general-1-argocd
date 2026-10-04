import base64
import copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "utility-apps/playground-previews/lifecycle"
SPEC = importlib.util.spec_from_file_location("preview_lifecycle", CHART / "files/lifecycle.py")
lifecycle = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = lifecycle
SPEC.loader.exec_module(lifecycle)


class GitAuthentication(unittest.TestCase):
    def test_jwt_is_short_lived_and_private_key_is_never_argument_content(self):
        signer = Mock(returncode=0, stdout=b"signature")
        with patch.object(lifecycle.subprocess, "run", return_value=signer) as execute:
            token = lifecycle.app_jwt("12345", "/mounted/private_key", now=10000)
        payload = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))
        self.assertEqual(payload, {"iat": 9940, "exp": 10540, "iss": "12345"})
        self.assertEqual(execute.call_args.args[0][-1], "/mounted/private_key")
        self.assertNotIn("shell", execute.call_args.kwargs)

    def test_jwt_rejects_invalid_app_id_and_hides_openssl_errors(self):
        with self.assertRaises(ValueError):
            lifecycle.app_jwt("123; env", "/unused")
        with patch.object(lifecycle.subprocess, "run", return_value=Mock(returncode=1, stderr=b"secret", stdout=b"")):
            with self.assertRaisesRegex(RuntimeError, "Unable to sign") as failure:
                lifecycle.app_jwt("123", "/mounted/key")
        self.assertNotIn("secret", str(failure.exception))

    def test_http_paths_cannot_target_another_host(self):
        client = lifecycle.GitHub("not-a-real-token")
        for path in ("https://evil.invalid", "//evil.invalid", "relative", "/api#fragment"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.request(path)

    def test_redirects_are_never_followed_with_authentication(self):
        with self.assertRaisesRegex(RuntimeError, "redirect"):
            lifecycle.NoRedirect().redirect_request(None, None, 302, "", {}, "https://elsewhere.invalid")


class GitAtomicUpdates(unittest.TestCase):
    def test_only_main_and_validated_paths_are_writable(self):
        api = Mock()
        with self.assertRaises(ValueError):
            lifecycle.GitDatabase(api, "feature/arbitrary")
        db = lifecycle.GitDatabase(api)
        with self.assertRaises(ValueError):
            db.commit("a" * 40, "b" * 40, {"apps/prod.yaml": None}, {"previews/state/example.json"})
        api.request.assert_not_called()

    def test_git_update_has_expected_parent_and_never_forces_ref(self):
        api = Mock()
        api.request.side_effect = [{"sha": "new-tree"}, {"sha": "new-commit"}, {}]
        db = lifecycle.GitDatabase(api)
        db.commit("old-head", "old-tree", {"previews/state/x.json": {"status": "expired"},
                  "previews/active/x.json": None}, {"previews/state/x.json", "previews/active/x.json"})
        tree_call, commit_call, ref_call = api.request.call_args_list
        self.assertEqual(tree_call.args[2]["base_tree"], "old-tree")
        self.assertEqual(commit_call.args[2]["parents"], ["old-head"])
        self.assertEqual(ref_call.args[2], {"sha": "new-commit", "force": False})
        self.assertEqual(tree_call.args[2]["tree"][0]["sha"], None)

    def test_truncated_tree_and_symlinks_fail_closed(self):
        api = Mock()
        api.request.side_effect = [{"object": {"sha": "head"}}, {"tree": {"sha": "tree"}},
                                   {"truncated": True, "tree": []}]
        with self.assertRaisesRegex(ValueError, "truncated"):
            lifecycle.GitDatabase(api).snapshot()
        with self.assertRaisesRegex(ValueError, "regular"):
            lifecycle.GitDatabase(api).read_json({"mode": "120000", "sha": "blob"})

    def test_json_blob_is_bounded_and_must_be_an_object(self):
        api = Mock()
        api.request.return_value = {"encoding": "base64", "size": 2, "content": base64.b64encode(b"[]").decode()}
        with self.assertRaisesRegex(ValueError, "JSON object"):
            lifecycle.GitDatabase(api).read_json({"mode": "100644", "sha": "blob"})


class LifecycleChart(unittest.TestCase):
    def test_chart_uses_minutely_nonroot_gitops_only_job(self):
        rendered = subprocess.run(["helm", "template", "lifecycle", str(CHART), "--namespace", "playground-previews"],
                                  check=True, stdout=subprocess.PIPE, text=True).stdout
        self.assertIn('schedule: "* * * * *"', rendered)
        self.assertIn("concurrencyPolicy: Forbid", rendered)
        self.assertIn("runAsNonRoot: true", rendered)
        self.assertIn("readOnlyRootFilesystem: true", rendered)
        self.assertIn("automountServiceAccountToken: false", rendered)
        self.assertIn("image: \"ghcr.io/actions/actions-runner:2.337.0@sha256:", rendered)
        self.assertNotIn("privileged: true", rendered)
        self.assertNotIn("hostPath:", rendered)
        self.assertIn("resources: [applications]", rendered)
        self.assertIn("resources: [namespaces]", rendered)
        self.assertNotIn("verbs: [create", rendered)
        self.assertNotIn("verbs: [delete", rendered)
        self.assertNotIn("verbs: [patch", rendered)
        self.assertIn("audience: vault", rendered)


class LeaseLifecycle(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
        self.namespace = lifecycle.preview_namespace("feature/shared-checkout")
        self.state_path = f"previews/state/{self.namespace}.json"
        self.active_path = f"previews/active/{self.namespace}.json"
        self.active = {"name": self.namespace, "namespace": self.namespace,
                       "branch": "feature/shared-checkout", "url": f"https://{self.namespace}.internal.api-api-api.com",
                       "sources": [{"repoURL": "https://github.com/anomaly51/general-1-argocd.git",
                                    "targetRevision": "a" * 40, "path": "apps/playground-shell",
                                    "helm": {"valuesObject": {"image": {"repository": "preview/shell", "tag": "preview@sha256:" + "d" * 64}}}}]}
        self.state = {"schema": 1, "namespace": self.namespace, "branch": self.active["branch"],
                      "url": self.active["url"], "generation": "first-generation", "phase": "starting",
                      "created_at": lifecycle.format_date(self.now - timedelta(minutes=5)),
                      "startup_deadline": lifecycle.format_date(self.now + timedelta(minutes=25)),
                      "expires_at": None, "ready_at": None,
                      "prs": {"shell": {"number": 10, "head": "b" * 40,
                                        "repository": "anomaly51/playground-shell"}},
                      "images": {"shell": {"head": "b" * 40, "digest": "sha256:" + "d" * 64}},
                      "baseline": {"shell": {"image": {"repository": "preview/shell", "tag": "staging@sha256:" + "e" * 64}}},
                      "plan_hash": lifecycle.hashlib.sha256(lifecycle.compact_json(self.active["sources"]).encode()).hexdigest()}
        self.application = {"spec": {"sources": copy.deepcopy(self.active["sources"])},
                            "status": {"sync": {"status": "Synced", "comparedTo": {
                                "sources": copy.deepcopy(self.active["sources"])}},
                                "health": {"status": "Healthy"},
                                "operationState": {"phase": "Succeeded"}}}
        self.github = Mock()
        self.github.request.return_value = {"number": 10, "state": "open",
                                             "author_association": "MEMBER",
                                             "base": {"ref": "main", "repo": {"full_name": "anomaly51/playground-shell"}},
                                             "head": {"ref": self.state["branch"], "sha": "b" * 40,
                                                      "repo": {"full_name": "anomaly51/playground-shell"}}}
        self.github.request.side_effect = self.github_response
        self.kubernetes = Mock()
        self.kubernetes.application.return_value = self.application
        self.kubernetes.namespace_exists.return_value = True
        self.check_http = Mock(return_value=True)

    def github_response(self, path):
        if "/pulls?" in path:
            return [self.github.request.return_value] if "/playground-shell/" in path else []
        return self.github.request.return_value

    def open_pr(self, service, number=20, head="c" * 40):
        repository = f"anomaly51/playground-{service}"
        return {"number": number, "state": "open", "author_association": "MEMBER",
                "head": {"ref": self.state["branch"], "sha": head, "repo": {"full_name": repository}},
                "base": {"ref": "main", "repo": {"full_name": repository}}}

    def listings(self, *pulls):
        return [[pr for pr in pulls if pr["base"]["repo"]["full_name"] == f"anomaly51/playground-{service}"]
                for service in sorted(lifecycle.SERVICES)]

    def plan(self, active=True):
        return lifecycle.plan_changes({self.state_path: self.state},
                                      {self.active_path: self.active} if active else {},
                                      self.github, self.kubernetes, self.now, check_http=self.check_http)

    def ready_state(self, expires_delta=15):
        self.state.update(phase="ready", ready_at=lifecycle.format_date(self.now - timedelta(minutes=1)),
                          expires_at=lifecycle.format_date(self.now + timedelta(minutes=expires_delta)))

    def test_branch_slug_collisions_are_prevented_and_namespace_bounded(self):
        names = {lifecycle.preview_namespace(branch) for branch in ("feature/a-b", "feature/a/b", "feature/A-B")}
        self.assertEqual(len(names), 3)
        self.assertLessEqual(len(lifecycle.preview_namespace("feature/" + "a" * 240)), 63)
        for branch in ("", "///", "feature/\ncommand"):
            with self.assertRaises(ValueError):
                lifecycle.preview_namespace(branch)

    def test_state_cannot_target_other_namespace_repository_or_arbitrary_file(self):
        self.assertEqual(lifecycle.validate_state(self.state_path, self.state), self.namespace)
        for field, value in (("namespace", "playground-prod"), ("branch", "feature/other"), ("schema", 2)):
            bad = {**self.state, field: value}
            with self.assertRaises(ValueError):
                lifecycle.validate_state(self.state_path, bad)
        with self.assertRaises(ValueError):
            lifecycle.validate_state("apps/prod.json", self.state)
        bad = copy.deepcopy(self.state)
        bad["prs"]["shell"]["repository"] = "outsider/playground-shell"
        with self.assertRaises(ValueError):
            lifecycle.validate_state(self.state_path, bad)

    def test_15_minute_clock_starts_only_after_exact_application_is_ready(self):
        changes = self.plan()
        self.assertEqual(set(changes), {self.state_path})
        self.assertEqual(changes[self.state_path]["phase"], "ready")
        self.assertEqual(changes[self.state_path]["ready_at"], "2026-10-05T12:00:00Z")
        self.assertEqual(changes[self.state_path]["expires_at"], "2026-10-05T12:15:00Z")
        self.assertEqual(self.state["phase"], "starting")

    def test_running_operation_old_sources_and_unsynced_state_never_start_timer(self):
        variants = []
        value = copy.deepcopy(self.application)
        value["status"]["operationState"]["phase"] = "Running"
        variants.append(value)
        value = copy.deepcopy(self.application)
        value["status"]["sync"]["comparedTo"]["sources"][0]["targetRevision"] = "old"
        variants.append(value)
        value = copy.deepcopy(self.application)
        value["spec"]["sources"][0]["targetRevision"] = "old"
        variants.append(value)
        value = copy.deepcopy(self.application)
        value["status"]["sync"]["status"] = "OutOfSync"
        variants.append(value)
        for value in variants + [None]:
            self.kubernetes.application.return_value = value
            self.assertEqual(self.plan(), {})

    def test_existing_ready_lease_is_not_renewed_by_polling(self):
        self.ready_state()
        self.assertEqual(self.plan(), {})
        self.kubernetes.application.assert_not_called()

    def test_update_in_progress_expires_at_existing_lease_deadline(self):
        self.ready_state(expires_delta=0)
        self.state["phase"] = "starting"
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "expired")
        self.assertIsNone(changes[self.active_path])
        self.github.request.assert_not_called()

    def test_successful_update_preserves_previous_ready_date_and_expiry(self):
        self.ready_state(expires_delta=7)
        self.state["phase"] = "starting"
        # A refreshed, previously usable preview can live longer than its initial startup window.
        self.state["startup_deadline"] = lifecycle.format_date(self.now - timedelta(minutes=30))
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "ready")
        self.assertEqual(changes[self.state_path]["ready_at"], self.state["ready_at"])
        self.assertEqual(changes[self.state_path]["expires_at"], self.state["expires_at"])

    def test_old_deployment_cannot_satisfy_new_pending_pr_head(self):
        self.state["prs"]["shell"]["head"] = "f" * 40
        self.assertEqual(self.plan(), {})
        self.kubernetes.application.assert_not_called()
        self.check_http.assert_not_called()

    def test_unrecorded_new_live_pr_head_cannot_mark_old_deployment_ready(self):
        self.github.request.return_value["head"]["sha"] = "f" * 40
        self.assertEqual(self.plan(), {})
        self.kubernetes.application.assert_not_called()
        self.check_http.assert_not_called()

    def test_ready_preview_with_new_live_head_becomes_pending_without_lease_extension(self):
        self.ready_state(expires_delta=7)
        self.github.request.return_value["head"]["sha"] = "f" * 40
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "starting")
        self.assertEqual(changes[self.state_path]["expires_at"], self.state["expires_at"])
        self.assertEqual(changes[self.state_path]["ready_at"], self.state["ready_at"])
        self.assertEqual(changes[self.state_path]["prs"], self.state["prs"])
        self.assertNotIn(self.active_path, changes)

    def test_new_same_branch_member_blocks_ready_before_its_ci_notification(self):
        self.ready_state(expires_delta=7)
        joining = self.open_pr("pricing-service")
        self.github.request.side_effect = lambda path: (
            [joining] if "/playground-pricing-service/pulls?" in path else self.github_response(path))
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "starting")
        for field in ("ready_at", "expires_at", "prs", "images"):
            self.assertEqual(changes[self.state_path][field], self.state[field])
        self.assertNotIn(self.active_path, changes)
        self.kubernetes.application.assert_not_called()
        self.check_http.assert_not_called()
        self.state = changes[self.state_path]
        self.assertEqual(self.plan(), {}, "Polling a pending join must not create repeated no-op commits")

    def test_initial_preview_does_not_start_ttl_while_an_unrecorded_member_is_pending(self):
        joining = self.open_pr("order-service")
        self.github.request.side_effect = lambda path: (
            [joining] if "/playground-order-service/pulls?" in path else self.github_response(path))
        self.assertEqual(self.plan(), {})
        self.assertIsNone(self.state["ready_at"])
        self.assertIsNone(self.state["expires_at"])
        self.kubernetes.application.assert_not_called()

    def test_live_membership_ignores_forks_other_branches_wrong_bases_and_untrusted_authors(self):
        candidates = []
        for field in ("fork", "branch", "base", "author", "closed"):
            pr = self.open_pr("pricing-service")
            if field == "fork":
                pr["head"]["repo"]["full_name"] = "outsider/playground-pricing-service"
            elif field == "branch":
                pr["head"]["ref"] = "feature/another"
            elif field == "base":
                pr["base"]["ref"] = "release/old"
            elif field == "author":
                pr["author_association"] = "CONTRIBUTOR"
            else:
                pr["state"] = "closed"
            candidates.append(pr)
        self.github.request.side_effect = lambda path: (
            candidates if "/playground-pricing-service/pulls?" in path else self.github_response(path))
        live = lifecycle.open_branch_prs(self.github, self.state)
        self.assertEqual(live, self.state["prs"])
        self.assertEqual(self.github.request.call_count, 8)

    def test_duplicate_or_invalid_eligible_live_pr_identity_is_rejected(self):
        first = self.open_pr("pricing-service")
        second = self.open_pr("pricing-service", number=21)
        for candidates in ([first, second], [{**first, "number": 0}],
                           [{**first, "head": {**first["head"], "sha": "main"}}]):
            with self.subTest(candidates=len(candidates)), self.assertRaises(ValueError):
                client = Mock(request=Mock(side_effect=lambda path: (
                    candidates if "/playground-pricing-service/pulls?" in path else [])))
                lifecycle.open_branch_prs(client, self.state)

    def test_one_group_membership_failure_does_not_block_other_groups_expiry(self):
        self.ready_state(expires_delta=7)
        expired = copy.deepcopy(self.state)
        expired.update(branch="feature/zzz-expired", namespace=lifecycle.preview_namespace("feature/zzz-expired"),
                       expires_at=lifecycle.format_date(self.now))
        expired_path = f"previews/state/{expired['namespace']}.json"
        expired_active_path = f"previews/active/{expired['namespace']}.json"
        for error in (lifecycle.APIError(403), ValueError("ambiguous with secret-value")):
            with self.subTest(error=type(error).__name__):
                self.github.request.side_effect = error
                changes = lifecycle.plan_changes(
                    {self.state_path: self.state, expired_path: expired},
                    {self.active_path: self.active, expired_active_path: {"still": "active"}},
                    self.github, self.kubernetes, self.now, check_http=self.check_http,
                )
                pending = changes[self.state_path]
                self.assertEqual(pending["phase"], "starting")
                self.assertEqual(pending["membership_error"], "Pull request membership could not be verified")
                self.assertNotIn("secret-value", json.dumps(changes))
                for field in ("ready_at", "expires_at", "prs", "images"):
                    self.assertEqual(pending[field], self.state[field])
                self.assertNotIn(self.active_path, changes)
                self.assertEqual(changes[expired_path]["phase"], "expired")
                self.assertIsNone(changes[expired_active_path])

    def test_membership_failure_does_not_create_repeat_commits_or_hide_deadline(self):
        self.ready_state(expires_delta=7)
        self.github.request.side_effect = lifecycle.APIError(403)
        self.state = self.plan()[self.state_path]
        self.assertEqual(self.plan(), {})
        self.now += timedelta(minutes=7)
        self.github.request.reset_mock()
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "expired")
        self.assertIsNone(changes[self.active_path])
        self.github.request.assert_not_called()

    def test_recovered_membership_clears_sanitized_error_without_renewing_lease(self):
        self.ready_state(expires_delta=7)
        self.github.request.side_effect = lifecycle.APIError(403)
        self.state = self.plan()[self.state_path]
        self.github.request.side_effect = self.github_response
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "ready")
        self.assertNotIn("membership_error", changes[self.state_path])
        self.assertEqual(changes[self.state_path]["expires_at"], self.state["expires_at"])

    def test_http_dependencies_must_be_usable_before_initial_ready(self):
        self.check_http.return_value = False
        self.assertEqual(self.plan(), {})
        self.check_http.assert_called_once_with(self.state)

    def test_expiration_removes_only_active_file_and_keeps_tombstone_without_api_dependency(self):
        self.ready_state(expires_delta=0)
        self.github.request.side_effect = RuntimeError("GitHub temporarily unavailable")
        changes = self.plan()
        self.assertIsNone(changes[self.active_path])
        self.assertEqual(changes[self.state_path]["phase"], "expired")
        self.assertEqual(changes[self.state_path]["generation"], "first-generation")
        self.github.request.assert_not_called()

    def test_startup_deadline_cleans_even_if_active_plan_was_never_written(self):
        self.state["startup_deadline"] = lifecycle.format_date(self.now)
        changes = self.plan(active=False)
        self.assertEqual(changes[self.state_path]["phase"], "failed")
        self.assertNotIn(self.active_path, changes)

    def test_one_open_pr_preserves_shared_preview(self):
        self.ready_state()
        self.state["prs"]["pricing-service"] = {"number": 11, "head": "c" * 40,
                                                 "repository": "anomaly51/playground-pricing-service"}
        self.state["images"]["pricing-service"] = {"head": "c" * 40, "digest": "sha256:" + "f" * 64}
        baseline = {"repository": "preview/pricing-service", "tag": "staging@sha256:" + "1" * 64}
        self.state["baseline"]["pricing-service"] = {"image": baseline}
        self.active["sources"].append({"path": "apps/playground-pricing-service", "helm": {
            "valuesObject": {"image": {"repository": "preview/pricing-service", "tag": "preview@sha256:" + "f" * 64}}}})
        self.github.request.side_effect = [
            {"number": 11, "state": "closed", "base": {"repo": {"full_name": "anomaly51/playground-pricing-service"}}},
            {"number": 10, "state": "open", "base": {"repo": {"full_name": "anomaly51/playground-shell"}}},
            *self.listings(self.github.request.return_value),
        ]
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "starting")
        self.assertEqual(set(changes[self.state_path]["prs"]), {"shell"})
        self.assertEqual(set(changes[self.state_path]["images"]), {"shell"})
        self.assertEqual(changes[self.active_path]["sources"][1]["helm"]["valuesObject"]["image"], baseline)
        self.assertEqual(changes[self.state_path]["expires_at"], self.state["expires_at"])
        self.assertIsNotNone(changes[self.active_path])

    def test_closed_pr_can_be_removed_before_an_active_manifest_exists(self):
        self.state["prs"]["pricing-service"] = {"number": 11, "head": "c" * 40,
                                                 "repository": "anomaly51/playground-pricing-service"}
        self.github.request.side_effect = [
            {"number": 11, "state": "closed", "base": {"repo": {"full_name": "anomaly51/playground-pricing-service"}}},
            {"number": 10, "state": "open", "base": {"repo": {"full_name": "anomaly51/playground-shell"}}},
            *self.listings(self.github.request.return_value),
        ]
        changes = self.plan(active=False)
        self.assertEqual(set(changes[self.state_path]["prs"]), {"shell"})
        self.assertNotIn(self.active_path, changes)

    def test_all_closed_prs_remove_preview_after_live_membership_check(self):
        self.github.request.side_effect = [
            {"number": 10, "state": "closed", "base": {"repo": {"full_name": "anomaly51/playground-shell"}}},
            *([] for _ in range(8)),
        ]
        changes = self.plan()
        self.assertIsNone(changes[self.active_path])
        self.assertEqual(changes[self.state_path]["phase"], "closed")

    def test_new_unrecorded_same_branch_pr_prevents_premature_group_deletion(self):
        self.ready_state()
        self.github.request.side_effect = [
            {"number": 10, "state": "closed", "base": {"repo": {"full_name": "anomaly51/playground-shell"}}},
            *self.listings(self.open_pr("analytics-service")),
        ]
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "starting")
        self.assertEqual(changes[self.state_path]["expires_at"], self.state["expires_at"])
        self.assertNotIn(self.active_path, changes)

    def test_expired_state_never_recreates_active_manifest_or_renews(self):
        self.state["phase"] = "expired"
        self.assertEqual(self.plan(active=False), {})
        self.github.request.assert_not_called()
        self.kubernetes.application.assert_not_called()

    def test_namespace_must_disappear_before_vault_cleanup_is_claimed(self):
        self.state["phase"] = "closed"
        self.assertEqual(self.plan(active=False), {})
        self.kubernetes.namespace_exists.return_value = False
        self.assertEqual(self.plan(active=False), {})
        self.kubernetes.application.return_value = None
        changes = self.plan(active=False)
        self.assertEqual(changes[self.state_path]["cleanup_started_at"], lifecycle.format_date(self.now))

    def test_absent_namespace_is_not_enough_while_manifest_still_exists_in_git(self):
        self.state["phase"] = "expired"
        self.kubernetes.namespace_exists.return_value = False
        self.kubernetes.application.return_value = None
        self.assertEqual(self.plan(), {self.active_path: None})

    def test_claimed_cleanup_waits_for_application_to_disappear_too(self):
        self.state.update(phase="expired", cleanup_started_at=lifecycle.format_date(self.now))
        self.kubernetes.namespace_exists.return_value = False
        database = Mock()
        database.snapshot.return_value = ("head", "tree", {self.state_path: {"path": self.state_path}})
        database.read_json.return_value = self.state
        vault = Mock()
        self.assertFalse(lifecycle.reconcile(database, self.github, self.kubernetes, vault, self.now))
        vault.cleanup.assert_not_called()
        self.kubernetes.application.return_value = None
        self.assertTrue(lifecycle.reconcile(database, self.github, self.kubernetes, vault, self.now))
        vault.cleanup.assert_called_once_with(self.state)

    def test_other_preview_changes_do_not_starve_durable_vault_cleanup(self):
        self.state.update(phase="closed", cleanup_started_at=lifecycle.format_date(self.now))
        expiring = copy.deepcopy(self.state)
        expiring.update(branch="feature/another-group", phase="ready",
                        ready_at=lifecycle.format_date(self.now - timedelta(minutes=15)),
                        expires_at=lifecycle.format_date(self.now), cleanup_started_at=None)
        expiring["namespace"] = lifecycle.preview_namespace(expiring["branch"])
        expiring_path = f"previews/state/{expiring['namespace']}.json"
        active_path = f"previews/active/{expiring['namespace']}.json"
        active = copy.deepcopy(self.active)
        active.update(name=expiring["namespace"], namespace=expiring["namespace"], branch=expiring["branch"])
        data = {self.state_path: self.state, expiring_path: expiring, active_path: active}
        database = Mock()
        database.snapshot.return_value = ("head", "tree", {path: {"path": path} for path in data})
        database.read_json.side_effect = lambda entry: data[entry["path"]]
        self.kubernetes.namespace_exists.return_value = False
        self.kubernetes.application.return_value = None
        vault = Mock()
        self.assertTrue(lifecycle.reconcile(database, self.github, self.kubernetes, vault, self.now))
        vault.cleanup.assert_called_once_with(self.state)
        changes = database.commit.call_args.args[2]
        self.assertEqual(changes[self.state_path]["cleanup_completed_at"], lifecycle.format_date(self.now))
        self.assertEqual(changes[expiring_path]["phase"], "expired")
        self.assertIsNone(changes[active_path])

    def test_new_cleanup_claim_is_committed_before_any_vault_deletion(self):
        self.state["phase"] = "closed"
        database = Mock()
        database.snapshot.return_value = ("head", "tree", {self.state_path: {"path": self.state_path}})
        database.read_json.return_value = self.state
        self.kubernetes.namespace_exists.return_value = False
        self.kubernetes.application.return_value = None
        vault = Mock()
        self.assertTrue(lifecycle.reconcile(database, self.github, self.kubernetes, vault, self.now))
        vault.cleanup.assert_not_called()
        changed = database.commit.call_args.args[2][self.state_path]
        self.assertEqual(changed["cleanup_started_at"], lifecycle.format_date(self.now))
        self.assertIsNone(changed.get("cleanup_completed_at"))

    def test_vault_provisioning_defers_cleanup_but_not_environment_expiration(self):
        self.ready_state(expires_delta=0)
        self.state["provisioning_until"] = lifecycle.format_date(self.now + timedelta(minutes=5))
        changes = self.plan()
        self.assertEqual(changes[self.state_path]["phase"], "expired")
        self.assertIsNone(changes[self.active_path])
        self.state = changes[self.state_path]
        self.kubernetes.namespace_exists.return_value = False
        self.kubernetes.application.return_value = None
        self.assertEqual(self.plan(active=False), {})
        self.state["provisioning_until"] = lifecycle.format_date(self.now)
        changes = self.plan(active=False)
        self.assertEqual(changes[self.state_path]["cleanup_started_at"], lifecycle.format_date(self.now))

    def test_operation_termination_requires_durable_terminal_state_without_active_manifest(self):
        database = Mock()
        paths = {self.state_path: {"path": self.state_path}, self.active_path: {"path": self.active_path}}
        database.snapshot.return_value = ("head", "tree", paths)
        self.ready_state(expires_delta=0)
        database.read_json.side_effect = [self.state, self.active]
        lifecycle.reconcile(database, self.github, self.kubernetes, Mock(), self.now)
        self.kubernetes.terminate_retired_operation.assert_not_called()
        self.state["phase"] = "expired"
        database.read_json.side_effect = [self.state, self.active]
        lifecycle.reconcile(database, self.github, self.kubernetes, Mock(), self.now)
        self.kubernetes.terminate_retired_operation.assert_not_called()
        database.snapshot.return_value = ("head", "tree", {self.state_path: {"path": self.state_path}})
        database.read_json.side_effect = [self.state]
        lifecycle.reconcile(database, self.github, self.kubernetes, Mock(), self.now)
        self.kubernetes.terminate_retired_operation.assert_called_once_with(self.state)

    def retired_application(self):
        value = copy.deepcopy(self.application)
        value.update(metadata={"name": self.namespace, "namespace": "argocd", "uid": "old-uid",
                               "resourceVersion": "123", "deletionTimestamp": lifecycle.format_date(self.now),
                               "labels": {"gitops.api-api-api.com/environment": "preview"}},
                     operation={"sync": {}})
        value["spec"]["destination"] = {"namespace": self.namespace}
        value["status"]["operationState"]["phase"] = "Running"
        return value

    def termination_client(self, application):
        client = lifecycle.Kubernetes.__new__(lifecycle.Kubernetes)
        client.token_path = "/unused-token"
        client.application = Mock(return_value=application)
        client.opener = Mock()
        client.opener.open.return_value = MagicMock()
        return client

    def test_retired_operation_patch_only_changes_status_with_uid_and_version_guards(self):
        self.state["phase"] = "expired"
        client = self.termination_client(self.retired_application())
        with patch.object(lifecycle.Path, "read_text", return_value="not-a-real-token"):
            self.assertTrue(client.terminate_retired_operation(self.state))
        request = client.opener.open.call_args.args[0]
        self.assertEqual(request.method, "PATCH")
        self.assertEqual(request.full_url, f"https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{self.namespace}")
        self.assertEqual(request.get_header("Content-type"), "application/json-patch+json")
        operations = json.loads(request.data)
        self.assertEqual(operations[:2], [{"op": "test", "path": "/metadata/uid", "value": "old-uid"},
                                         {"op": "test", "path": "/metadata/resourceVersion", "value": "123"}])
        self.assertEqual(operations[-2:], [{"op": "test", "path": "/status/operationState/phase", "value": "Running"},
                                          {"op": "replace", "path": "/status/operationState/phase", "value": "Terminating"}])

    def test_operation_termination_never_touches_active_or_already_finished_application(self):
        application = self.retired_application()
        client = self.termination_client(application)
        self.assertFalse(client.terminate_retired_operation(self.state))
        client.application.assert_not_called()
        self.state["phase"] = "expired"
        variants = [None]
        for phase in ("Succeeded", "Failed", "Terminating"):
            value = copy.deepcopy(application)
            value["status"]["operationState"]["phase"] = phase
            variants.append(value)
        value = copy.deepcopy(application)
        del value["metadata"]["deletionTimestamp"]
        variants.append(value)
        value = copy.deepcopy(application)
        del value["operation"]
        variants.append(value)
        for value in variants:
            client.application.return_value = value
            self.assertFalse(client.terminate_retired_operation(self.state))
        client.opener.open.assert_not_called()

    def test_termination_rejects_foreign_application_and_handles_concurrent_completion(self):
        self.state["phase"] = "expired"
        application = self.retired_application()
        application["spec"]["destination"]["namespace"] = "playground-prod"
        client = self.termination_client(application)
        with self.assertRaisesRegex(ValueError, "identity"):
            client.terminate_retired_operation(self.state)
        client.opener.open.assert_not_called()
        client.application.return_value = self.retired_application()
        for status in (404, 409, 422):
            client.opener.open.side_effect = lifecycle.urllib.error.HTTPError("redacted", status, "", {}, None)
            with patch.object(lifecycle.Path, "read_text", return_value="not-a-real-token"):
                self.assertFalse(client.terminate_retired_operation(self.state))

    def test_cas_retry_rereads_refreshed_expiry_instead_of_replaying_deletion(self):
        self.ready_state(expires_delta=-1)
        refreshed = copy.deepcopy(self.state)
        refreshed.update(generation="refreshed-generation", expires_at=lifecycle.format_date(self.now + timedelta(minutes=15)))
        database = Mock()
        paths = {self.state_path: {"path": self.state_path}, self.active_path: {"path": self.active_path}}
        database.snapshot.side_effect = [("old", "tree1", paths), ("new", "tree2", paths)]
        database.read_json.side_effect = [self.state, self.active, refreshed, self.active]
        database.commit.side_effect = lifecycle.APIError(422)
        with patch.object(lifecycle.time, "sleep"):
            self.assertFalse(lifecycle.reconcile(database, self.github, self.kubernetes, Mock(), self.now))
        self.assertEqual(database.commit.call_count, 1)

    def test_vault_cleanup_is_idempotent_scoped_and_requires_durable_claim(self):
        vault = lifecycle.Vault()
        vault.token = "not-a-real-token"
        vault._request = Mock()
        with self.assertRaises(ValueError):
            vault.cleanup(self.state)
        self.state.update(phase="expired", cleanup_started_at=lifecycle.format_date(self.now))
        vault.cleanup(self.state)
        paths = [call.args[0] for call in vault._request.call_args_list]
        self.assertEqual(paths, [f"kv/metadata/apps/{self.namespace}/registry", f"kv/metadata/apps/{self.namespace}",
                                 f"auth/kubernetes/role/{self.namespace}", f"sys/policies/acl/{self.namespace}"])
        self.assertTrue(all(call.args[1] == "DELETE" for call in vault._request.call_args_list))

    def test_changed_active_sources_fail_plan_hash_check(self):
        lifecycle.validate_active(self.state, self.active)
        bad = copy.deepcopy(self.active)
        bad["sources"][0]["targetRevision"] = "different"
        with self.assertRaisesRegex(ValueError, "recorded plan"):
            lifecycle.validate_active(self.state, bad)

    def test_http_ready_verifies_brokers_even_when_endpoint_returns_200(self):
        api = {"status": "ready", "dependencies": {key: True for key in ("processor", "postgres", "redis", "brokers")}}
        events = {"status": "ready", "dependencies": {"kafka": True, "rabbitmq": True}}

        def response(body):
            result = MagicMock()
            result.__enter__.return_value = result
            result.status = 200
            result.read.return_value = json.dumps(body).encode()
            return result

        opener = Mock()
        opener.open.side_effect = [response(api), response(events)]
        with patch.object(lifecycle.urllib.request, "build_opener", return_value=opener):
            self.assertTrue(lifecycle.http_ready(self.state))
        self.assertTrue(all(call.kwargs["timeout"] == 5 for call in opener.open.call_args_list))
        self.assertTrue(all(call.args[0].get_header("Authorization") is None for call in opener.open.call_args_list))
        api["dependencies"]["brokers"] = False
        opener.open.side_effect = [response(api)]
        with patch.object(lifecycle.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(lifecycle.http_ready(self.state))

    def test_http_ready_rejects_foreign_urls_redirects_and_oversized_payloads(self):
        bad = {**self.state, "url": "https://other.internal.api-api-api.com"}
        with self.assertRaises(ValueError):
            lifecycle.http_ready(bad)
        opener = Mock()
        opener.open.side_effect = RuntimeError("Unexpected API redirect rejected")
        with patch.object(lifecycle.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(lifecycle.http_ready(self.state))
        result = MagicMock()
        result.__enter__.return_value = result
        result.status = 200
        result.read.return_value = b"a" * 65537
        opener.open.side_effect = [result]
        with patch.object(lifecycle.urllib.request, "build_opener", return_value=opener):
            self.assertFalse(lifecycle.http_ready(self.state))


if __name__ == "__main__":
    unittest.main()
