"""Offline contracts for feature membership, Git CAS and isolated preview plans."""

import base64
import copy
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, mock_open, patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))
import playground_preview as preview


BRANCH = "feature/discounts"
HEAD = "a" * 40
REVISION = "b" * 40
DIGEST = "sha256:" + "c" * 64
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)


def pr_identity(service="shell", head=HEAD, number=42):
    return {"number": number, "head": head, "repository": f"anomaly51/playground-{service}"}


def github_pr(service="shell", branch=BRANCH, head=HEAD, number=42):
    repository = f"anomaly51/playground-{service}"
    return {"number": number, "author_association": "MEMBER", "state": "open",
            "head": {"ref": branch, "sha": head, "repo": {"full_name": repository}},
            "base": {"ref": "main", "repo": {"full_name": repository}}}


def ci_run(service="shell", head=HEAD):
    return {"id": 123, "path": ".github/workflows/ci.yaml", "event": "pull_request",
            "status": "completed", "conclusion": "success", "head_sha": head,
            "head_branch": BRANCH,
            "head_repository": {"full_name": f"anomaly51/playground-{service}"}}


def new_state(prs=None):
    return preview.prepare_state(None, None, BRANCH, prs or {"shell": pr_identity()},
                                 False, REVISION, preview.baseline_snapshot(), now=NOW,
                                 utilities=preview.utility_snapshot())[0]


class PreviewIdentityAndMembershipTests(unittest.TestCase):
    def test_branch_namespace_is_deterministic_dns_safe_and_collision_resistant(self):
        name = preview.namespace_for(BRANCH)
        self.assertEqual(name, preview.namespace_for(BRANCH))
        self.assertLessEqual(len(name), 63)
        self.assertRegex(name, r"^playground-preview-[a-z0-9-]+-[0-9a-f]{10}$")
        self.assertNotEqual(preview.namespace_for("feature/a-b"), preview.namespace_for("feature/a/b"))
        self.assertNotEqual(preview.namespace_for("feature/Test"), preview.namespace_for("feature/test"))
        self.assertNotEqual(preview.namespace_for("feature/" + "a" * 160 + "x"),
                            preview.namespace_for("feature/" + "a" * 160 + "y"))

    def test_only_bounded_feature_branch_names_are_accepted(self):
        for branch in (None, "", "main", "dev", "hotfix/test", "feature/", "feature/test/",
                       "feature/a\nb", "feature/" + "a" * 180):
            with self.subTest(branch=branch), self.assertRaises(ValueError):
                preview.namespace_for(branch)

    def test_same_branch_in_four_repositories_becomes_one_membership(self):
        services = ("shell", "traffic-mfe", "order-service", "pricing-service")
        github = Mock()
        github.request.side_effect = lambda path: [github_pr(service)] if (
            service := path.split("/")[3].removeprefix("playground-")) in services else []
        result = preview.current_prs(github, BRANCH)
        self.assertEqual(set(result), set(services))
        self.assertEqual(github.request.call_count, 8)
        for service in services:
            self.assertEqual(result[service], pr_identity(service))

    def test_foreign_branches_are_ignored_and_duplicate_membership_is_rejected(self):
        github = Mock()
        github.request.return_value = [github_pr(branch="feature/unrelated")]
        self.assertEqual(preview.current_prs(github, BRANCH), {})
        github.request.return_value = [github_pr("order-service"), github_pr("order-service", number=43)]
        with self.assertRaisesRegex(ValueError, "Ambiguous"):
            preview.current_prs(github, BRANCH)

    def test_forks_untrusted_authors_and_wrong_base_are_never_members(self):
        for field in ("fork", "base-repository", "author", "base-branch", "sha"):
            pr = github_pr("order-service")
            if field == "fork":
                pr["head"]["repo"]["full_name"] = "outsider/playground-order-service"
            elif field == "base-repository":
                pr["base"]["repo"]["full_name"] = "outsider/other"
            elif field == "author":
                pr["author_association"] = "CONTRIBUTOR"
            elif field == "base-branch":
                pr["base"]["ref"] = "release/old"
            else:
                pr["head"]["sha"] = "main"
            with self.subTest(field=field):
                github = Mock(request=Mock(return_value=[pr]))
                if field == "sha":
                    with self.assertRaises(ValueError):
                        preview.current_prs(github, BRANCH)
                else:
                    self.assertEqual(preview.current_prs(github, BRANCH), {})

    def test_ci_gating_checks_exact_head_and_exact_head_checkout_configuration(self):
        github = Mock()
        workflow = yaml.safe_dump({"jobs": {
            name: {"steps": [{"uses": "actions/checkout@pinned", "with": {
                "ref": "${{ github.event.pull_request.head.sha || github.sha }}"}}]}
            for name in ("tests", "build")
        }})
        github.request.side_effect = [{"workflow_runs": [ci_run()]},
                                      {"content": base64.b64encode(workflow.encode()).decode()}]
        self.assertEqual(preview.successful_ci(github, "shell", pr_identity()), 123)
        self.assertIn("head_sha=" + HEAD, github.request.call_args_list[0].args[0])
        self.assertIn("?ref=" + HEAD, github.request.call_args_list[1].args[0])
        for field, value in (("head_sha", "d" * 40), ("conclusion", "failure"),
                             ("status", "in_progress"), ("event", "push"),
                             ("path", ".github/workflows/unrelated.yaml"),
                             ("head_repository", {"full_name": "outsider/fork"})):
            run = ci_run()
            run[field] = value
            with self.subTest(field=field):
                client = Mock(request=Mock(return_value={"workflow_runs": [run]}))
                self.assertIsNone(preview.successful_ci(client, "shell", pr_identity()))
                self.assertEqual(client.request.call_count, 1)
        github.request.side_effect = [{"workflow_runs": [ci_run()]},
                                      {"content": base64.b64encode(b"ref: main").decode()}]
        with self.assertRaisesRegex(ValueError, "exact-head"):
            preview.successful_ci(github, "shell", pr_identity())

    def test_notifications_are_verified_against_the_actual_successful_ci_run(self):
        event = {"client_payload": {"service": "shell", "run_id": 123, "branch": "feature/spoofed"}}
        github = Mock(request=Mock(return_value=ci_run()))
        self.assertEqual(preview.event_branch(github, event), (BRANCH, "shell", HEAD))
        for payload in ({"service": "unknown", "run_id": 123}, {"service": "shell", "run_id": "../123"}):
            with self.assertRaises(ValueError):
                preview.event_branch(github, {"client_payload": payload})
        bad = ci_run()
        bad["conclusion"] = "failure"
        with self.assertRaises(ValueError):
            preview.event_branch(Mock(request=Mock(return_value=bad)), event)


class PreviewPreparationTests(unittest.TestCase):
    def test_new_preview_has_separate_startup_deadline_but_no_running_lease_yet(self):
        state = new_state()
        self.assertEqual(state["phase"], "starting")
        self.assertIsNone(state["ready_at"])
        self.assertIsNone(state["expires_at"])
        self.assertEqual(preview.timestamp(state["startup_deadline"]), NOW + timedelta(minutes=30))
        self.assertEqual(set(state["baseline"]), set(preview.SERVICES))
        self.assertEqual(set(state["utilities"]), set(preview.UTILITIES))
        self.assertEqual(state["revision"], REVISION)

    def test_ready_refresh_changes_generation_and_extends_lease_fifteen_minutes(self):
        previous = new_state()
        previous.update(phase="ready", ready_at=preview.now_string(NOW),
                        expires_at=preview.now_string(NOW + timedelta(minutes=15)))
        moment = NOW + timedelta(minutes=10)
        refreshed, active = preview.prepare_state(previous, {"existing": True}, BRANCH, previous["prs"],
            True, "e" * 40, {"ignored": True}, now=moment, utilities={"ignored": True})
        self.assertNotEqual(refreshed["generation"], previous["generation"])
        self.assertEqual(preview.timestamp(refreshed["expires_at"]), moment + timedelta(minutes=15))
        for key in ("baseline", "utilities", "revision"):
            self.assertEqual(refreshed[key], previous[key])
        self.assertEqual(active, {"existing": True})
        self.assertEqual(preview.timestamp(previous["expires_at"]), NOW + timedelta(minutes=15))

    def test_pending_refresh_extends_only_startup_deadline(self):
        previous = new_state()
        moment = NOW + timedelta(minutes=20)
        refreshed, _ = preview.prepare_state(previous, None, BRANCH, previous["prs"], True,
            REVISION, previous["baseline"], now=moment, utilities=previous["utilities"])
        self.assertEqual(preview.timestamp(refreshed["startup_deadline"]), moment + timedelta(minutes=30))
        self.assertIsNone(refreshed["expires_at"])

    def test_terminal_preview_requires_explicit_refresh_after_completed_cleanup(self):
        for phase in preview.INACTIVE:
            previous = new_state()
            previous["phase"] = phase
            for refresh in (False, True):
                with self.subTest(phase=phase, refresh=refresh), self.assertRaises(ValueError):
                    preview.prepare_state(previous, None, BRANCH, previous["prs"], refresh,
                                          REVISION, previous["baseline"], now=NOW)
            previous["cleanup_completed_at"] = preview.now_string(NOW)
            restored, active = preview.prepare_state(previous, None, BRANCH, previous["prs"], True,
                REVISION, previous["baseline"], now=NOW, utilities=previous["utilities"])
            self.assertEqual(restored["phase"], "starting")
            self.assertNotEqual(restored["generation"], previous["generation"])
            self.assertNotIn("cleanup_completed_at", restored)
            self.assertIsNone(active)

    def test_identity_mismatch_and_expired_lease_cannot_be_reconciled_implicitly(self):
        previous = new_state()
        previous["namespace"] = "playground-prod"
        with self.assertRaisesRegex(ValueError, "identity"):
            preview.prepare_state(previous, None, BRANCH, previous["prs"], False,
                                  REVISION, previous["baseline"], now=NOW)
        previous = new_state()
        previous.update(phase="ready", ready_at=preview.now_string(NOW - timedelta(minutes=16)),
                        expires_at=preview.now_string(NOW))
        with self.assertRaisesRegex(ValueError, "expired"):
            preview.prepare_state(previous, None, BRANCH, previous["prs"], False,
                                  REVISION, previous["baseline"], now=NOW)

    def test_changed_membership_leaves_ready_phase_and_preserves_frozen_baseline(self):
        previous = new_state()
        previous.update(phase="ready", ready_at=preview.now_string(NOW),
                        expires_at=preview.now_string(NOW + timedelta(minutes=15)))
        prs = {"shell": pr_identity(), "order-service": pr_identity("order-service", "d" * 40)}
        result, _ = preview.prepare_state(previous, {"old": True}, BRANCH, prs, False,
            "f" * 40, {"ignored": True}, now=NOW + timedelta(minutes=1), utilities={"ignored": True})
        self.assertEqual(result["phase"], "starting")
        self.assertEqual(result["prs"], prs)
        self.assertEqual(result["baseline"], previous["baseline"])
        self.assertEqual(result["utilities"], previous["utilities"])
        # A normal source update does not silently prolong the existing lease.
        self.assertEqual(result["expires_at"], previous["expires_at"])
        self.assertEqual(result["ready_at"], previous["ready_at"])


class PreviewGitCASTests(unittest.TestCase):
    def test_conflict_reloads_state_before_retry_and_preserves_concurrent_refresh(self):
        namespace = preview.namespace_for(BRANCH)
        state_path = f"previews/state/{namespace}.json"
        active_path = f"previews/active/{namespace}.json"
        database = Mock()
        database.snapshot.side_effect = [("a" * 40, "b" * 40, {state_path: {"version": 1}}),
                                         ("c" * 40, "d" * 40, {state_path: {"version": 2}})]
        database.read_json.side_effect = [{"generation": "old", "expires_at": "old"},
                                          {"generation": "refreshed", "expires_at": "new"}]
        database.commit.side_effect = [preview.APIError(409), True]
        observed = []

        def mutate(state, active):
            observed.append(state["generation"])
            return {**state, "checked": True}, active

        with patch.object(preview.time, "sleep"):
            result = preview.atomic_state(database, namespace, mutate)
        self.assertEqual(observed, ["old", "refreshed"])
        self.assertEqual(result["expires_at"], "new")
        latest = database.commit.call_args.args
        self.assertEqual(latest[0:2], ("c" * 40, "d" * 40))
        self.assertEqual(latest[3], {state_path, active_path})
        self.assertEqual(set(latest[2]), {state_path})

    def test_noop_does_not_commit_and_non_conflict_failure_does_not_retry(self):
        database = Mock()
        database.snapshot.return_value = ("head", "tree", {})
        self.assertIsNone(preview.atomic_state(database, preview.namespace_for(BRANCH), lambda old, active: (old, active)))
        database.commit.assert_not_called()
        database.commit.side_effect = preview.APIError(403)
        with self.assertRaises(preview.APIError), patch.object(preview.time, "sleep") as sleep:
            preview.atomic_state(database, preview.namespace_for(BRANCH), lambda old, active: ({"new": True}, None))
        sleep.assert_not_called()


class PreviewSourceIsolationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = new_state()
        cls.state["images"] = {"shell": {"head": HEAD, "digest": DIGEST, "run_id": 123}}
        cls.sources = preview.sources_for(cls.state, REVISION)
        cls.documents = []
        for source in cls.sources:
            output = subprocess.run(
                ["helm", "template", source["helm"]["releaseName"], str(ROOT / source["path"]),
                 "--namespace", cls.state["namespace"], "--values", "-"],
                input=yaml.safe_dump(source["helm"]["valuesObject"]), text=True,
                capture_output=True, check=True,
            ).stdout
            cls.documents.extend(document for document in yaml.safe_load_all(output) if document)

    def test_one_complete_plan_uses_only_trusted_paths_and_pinned_chart_revisions(self):
        self.assertEqual(len(self.sources), 15)
        paths = {f"apps/playground-{service}" for service in preview.SERVICES}
        paths |= {f"utility-apps/playground-staging/{utility}" for utility in preview.UTILITIES}
        self.assertEqual({source["path"] for source in self.sources}, paths)
        for source in self.sources:
            self.assertEqual(source["repoURL"], preview.REPO_URL)
            self.assertEqual(source["targetRevision"], REVISION)
            self.assertNotIn("_release", source["helm"]["valuesObject"])
        identities = [(doc["apiVersion"], doc["kind"], doc["metadata"]["name"]) for doc in self.documents]
        self.assertEqual(len(identities), len(set(identities)))
        for doc in self.documents:
            if doc["kind"] != "Namespace":
                self.assertEqual(doc["metadata"].get("namespace", self.state["namespace"]), self.state["namespace"])

    def test_only_selected_components_change_and_snapshots_are_not_mutated(self):
        for source in self.sources[:8]:
            service = source["path"].removeprefix("apps/playground-")
            actual = source["helm"]["valuesObject"]["image"]["tag"]
            expected = "preview@" + DIGEST if service == "shell" else self.state["baseline"][service]["image"]["tag"]
            self.assertEqual(actual, expected)
        before = copy.deepcopy(self.state)
        self.assertEqual(preview.sources_for(self.state, REVISION), self.sources)
        self.assertEqual(self.state, before)
        self.assertFalse(self.state["utilities"]["namespace"]["ephemeral"])
        self.assertEqual(self.state["utilities"]["edge"]["hostname"], "playground-staging.internal.api-api-api.com")

    def test_preview_hostname_cors_secrets_and_data_all_remain_namespace_local(self):
        routes = [doc for doc in self.documents if doc["kind"] == "HTTPRoute"]
        self.assertEqual(routes[0]["spec"]["hostnames"], [self.state["url"].removeprefix("https://")])
        for source in self.sources[:8]:
            values = source["helm"]["valuesObject"]
            if "CORS_ORIGINS" in values.get("env", {}):
                self.assertEqual(values["env"]["CORS_ORIGINS"], self.state["url"])
        secrets = [doc for doc in self.documents if doc["kind"] == "VaultStaticSecret"]
        self.assertEqual(len(secrets), 7)
        for secret in secrets:
            self.assertIn(secret["spec"]["path"], {
                "apps/" + self.state["namespace"], "apps/" + self.state["namespace"] + "/registry",
            })
        text = yaml.safe_dump(self.documents)
        self.assertNotIn("apps/playground-staging", text)
        self.assertNotIn("apps/playground-prod", text)
        self.assertNotIn("Prune=false", text)
        self.assertNotIn("Delete=false", text)
        self.assertNotIn("Retain", text)

    def test_mutable_revision_and_mutable_override_digest_are_rejected(self):
        with self.assertRaises(ValueError):
            preview.sources_for(self.state, "main")
        state = copy.deepcopy(self.state)
        state["images"]["shell"]["digest"] = "latest"
        with self.assertRaises(ValueError):
            preview.sources_for(state, REVISION)

    def test_all_four_requested_change_combinations_share_the_same_composition_rule(self):
        cases = (("shell",), ("pricing-service",), ("shell", "pricing-service"),
                 ("shell", "traffic-mfe", "order-service", "pricing-service"))
        for changed in cases:
            with self.subTest(changed=changed):
                state = copy.deepcopy(self.state)
                state["images"] = {service: {"head": HEAD, "digest": DIGEST, "run_id": 123}
                                   for service in changed}
                sources = preview.sources_for(state, REVISION)
                self.assertEqual(len(sources), 15)
                for source in sources[:8]:
                    service = source["path"].removeprefix("apps/playground-")
                    tag = source["helm"]["valuesObject"]["image"]["tag"]
                    self.assertEqual(tag, "preview@" + DIGEST if service in changed
                                     else state["baseline"][service]["image"]["tag"])


class PreviewRuntimeCredentialTests(unittest.TestCase):
    def test_runtime_uses_own_cas_created_secrets_and_preserves_them_on_repeat(self):
        namespace = preview.namespace_for(BRANCH)
        vault = preview.Vault.__new__(preview.Vault)
        stored = {"kv/data/apps/playground-prod/registry": {
            "username": "readonly-example", "password": "not-a-real-secret"}}

        def request(path, method="GET", data=None, **kwargs):
            if method == "GET":
                return {"data": {"data": copy.deepcopy(stored[path])}} if path in stored else None
            if path.startswith("kv/data/"):
                self.assertEqual(data["options"], {"cas": 0})
                self.assertNotIn(path, stored)
                stored[path] = copy.deepcopy(data["data"])
            return None

        vault.request = Mock(side_effect=request)
        vault.runtime(namespace)
        first = copy.deepcopy(stored)
        vault.runtime(namespace)
        self.assertEqual(stored, first)
        self.assertEqual(set(stored), {"kv/data/apps/playground-prod/registry",
                                     f"kv/data/apps/{namespace}", f"kv/data/apps/{namespace}/registry"})
        writes = [call for call in vault.request.call_args_list
                  if len(call.args) > 1 and call.args[1] != "GET"]
        data_writes = [call for call in writes if call.args[0].startswith("kv/data/")]
        self.assertEqual(len(data_writes), 2)
        role = next(call.args[2] for call in writes if call.args[0].startswith("auth/kubernetes/role/"))
        self.assertEqual(role["bound_service_account_namespaces"], [namespace])
        self.assertEqual(role["bound_service_account_names"], ["playground-runtime"])
        self.assertEqual(role["token_policies"], [namespace])
        self.assertTrue(role["token_no_default_policy"])
        policy = next(call.args[2]["policy"] for call in writes if call.args[0].startswith("sys/policies/acl/"))
        self.assertIn(f'path "kv/data/apps/{namespace}"', policy)
        self.assertIn(f'path "kv/data/apps/{namespace}/registry"', policy)
        self.assertNotIn("playground-prod", policy)
        self.assertNotIn('capabilities = ["write"]', policy)

    def test_cached_image_requires_matching_commit_and_source_repository(self):
        vault = Mock()
        vault.get.return_value = {"username": "readonly-example", "password": "not-a-real-secret"}
        prior = {"head": HEAD, "digest": DIGEST}
        with patch.dict(os.environ, {}, clear=False), patch.object(preview, "Registry") as registry, \
                patch.object(preview, "command") as command:
            registry.return_value.image.return_value = (DIGEST, "anomaly51/playground-shell")
            image = preview.build_image("shell", pr_identity(), 123, vault, prior)
            self.assertEqual(image, {"head": HEAD, "digest": DIGEST, "run_id": 123})
            registry.return_value.image.assert_called_once_with(DIGEST, HEAD, require_main_label=False)
            command.assert_not_called()
            registry.return_value.image.return_value = (DIGEST, "anomaly51/playground-order-service")
            with self.assertRaisesRegex(ValueError, "repository"):
                preview.build_image("shell", pr_identity(), 123, vault, prior)


class PreviewOrchestrationGuardTests(unittest.TestCase):
    def test_readiness_job_is_hosted_read_only_and_has_no_vault_or_build_access(self):
        workflow = yaml.safe_load((ROOT / ".github/workflows/preview-playground.yaml").read_text())
        build, ready = workflow["jobs"]["preview"], workflow["jobs"]["ready"]
        self.assertEqual(build["runs-on"], "arc-runner-set")
        self.assertEqual(ready["runs-on"], "ubuntu-latest")
        self.assertNotIn("concurrency", workflow)
        self.assertEqual(build["concurrency"]["group"], "playground-preview-orchestration")
        self.assertNotIn("concurrency", ready)
        self.assertEqual(ready["needs"], "preview")
        self.assertEqual(ready["if"], "needs.preview.outputs.deployed == 'true'")
        self.assertEqual(ready["permissions"], {"contents": "read"})
        for key in ("deployed", "namespace", "generation"):
            self.assertEqual(build["outputs"][key], "${{ steps.compose.outputs." + key + " }}")
        text = yaml.safe_dump(ready)
        for forbidden in ("vault-action", "create-github-app-token", "setup-buildx", "id-token", "REGISTRY_PASSWORD"):
            self.assertNotIn(forbidden, text)
        step = ready["steps"][-1]
        self.assertEqual(step["env"]["GH_TOKEN"], "${{ github.token }}")
        self.assertIn('--wait-namespace "$PREVIEW_NAMESPACE" --generation "$PREVIEW_GENERATION"', step["run"])

    def test_compose_exports_readiness_identity_without_waiting_on_arc(self):
        state = new_state()
        environment = {"GITHUB_REPOSITORY": preview.REPOSITORY, "GITHUB_REF": "refs/heads/main",
                       "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_OUTPUT": "/fake/job-output"}
        writer = mock_open()
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(sys, "argv", ["preview.py", "--branch", BRANCH]), \
                patch.object(preview, "configure_preview", return_value=(state, True)) as compose, \
                patch.object(preview, "wait_ready") as wait, patch("builtins.open", writer):
            preview.main()
        compose.assert_called_once_with(BRANCH, False, None)
        wait.assert_not_called()
        writer.assert_called_once_with("/fake/job-output", "a")
        writer().write.assert_called_once_with(
            f"deployed=true\nnamespace={state['namespace']}\ngeneration={state['generation']}\n")

    def test_wait_identity_is_validated_before_git_access_and_never_provisions_secrets(self):
        state = new_state()
        environment = {"GITHUB_REPOSITORY": preview.REPOSITORY, "GITHUB_REF": "refs/heads/main",
                       "GITHUB_EVENT_NAME": "workflow_dispatch"}
        for namespace, generation in (("playground-prod", "valid"), ("../../state", "valid"),
                                      (state["namespace"], "")):
            with self.subTest(namespace=namespace, generation=generation), \
                    patch.dict(os.environ, environment, clear=True), \
                    patch.object(sys, "argv", ["preview.py", "--wait-namespace", namespace,
                                               "--generation", generation]), \
                    patch.object(preview, "GitHub") as github, \
                    patch.object(preview, "configure_preview") as compose, patch.object(preview, "Vault") as vault:
                with self.assertRaisesRegex(ValueError, "readiness identity"):
                    preview.main()
                github.assert_not_called()
                compose.assert_not_called()
                vault.assert_not_called()
        with patch.dict(os.environ, environment, clear=True), \
                patch.object(sys, "argv", ["preview.py", "--wait-namespace", state["namespace"],
                                           "--generation", state["generation"]]), \
                patch.object(preview, "wait_ready", return_value=state) as wait, \
                patch.object(preview, "configure_preview") as compose, patch.object(preview, "Vault") as vault:
            preview.main()
        wait.assert_called_once_with(state["namespace"], state["generation"])
        compose.assert_not_called()
        vault.assert_not_called()

    def test_pending_component_blocks_all_builds_and_active_publication(self):
        prs = {"shell": pr_identity(), "order-service": pr_identity("order-service")}
        state = new_state(prs)
        database = Mock()
        database.snapshot.return_value = ("head", "tree", {"previews/config.json": {"sha": "config"}})
        database.read_json.return_value = {"enabled": True, "chart_revision": REVISION}
        with patch.dict(os.environ, {"GH_TOKEN": "not-a-real-token"}), \
                patch.object(preview, "GitHub"), patch.object(preview, "GitDatabase", return_value=database), \
                patch.object(preview, "current_prs", return_value=prs), \
                patch.object(preview, "atomic_state", return_value=state) as atomic, \
                patch.object(preview, "successful_ci", side_effect=[123, None]), \
                patch.object(preview, "Vault") as vault, patch.object(preview, "build_image") as build:
            actual, deployed = preview.configure_preview(BRANCH, False)
        self.assertEqual(actual, state)
        self.assertFalse(deployed)
        self.assertEqual(atomic.call_count, 1)
        vault.assert_not_called()
        build.assert_not_called()

    def test_disabled_configuration_fails_before_membership_or_secret_access(self):
        database = Mock()
        database.snapshot.return_value = ("head", "tree", {"previews/config.json": {"sha": "config"}})
        database.read_json.return_value = {"enabled": False, "chart_revision": REVISION}
        with patch.dict(os.environ, {"GH_TOKEN": "not-a-real-token"}), \
                patch.object(preview, "GitHub"), patch.object(preview, "GitDatabase", return_value=database), \
                patch.object(preview, "current_prs") as membership, patch.object(preview, "Vault") as vault:
            with self.assertRaisesRegex(ValueError, "not enabled"):
                preview.configure_preview(BRANCH, False)
        membership.assert_not_called()
        vault.assert_not_called()

    def test_caller_must_be_trusted_gitops_main_manual_or_dispatch_workflow(self):
        valid = {"GITHUB_REPOSITORY": preview.REPOSITORY, "GITHUB_REF": "refs/heads/main",
                 "GITHUB_EVENT_NAME": "workflow_dispatch"}
        for key, invalid in (("GITHUB_REPOSITORY", "outsider/repo"), ("GITHUB_REF", "refs/heads/dev"),
                             ("GITHUB_EVENT_NAME", "pull_request")):
            with patch.dict(os.environ, {**valid, key: invalid}), patch.object(sys, "argv", ["preview.py"]), \
                    patch.object(preview, "configure_preview") as configure, self.assertRaises(ValueError):
                preview.main()
            configure.assert_not_called()


if __name__ == "__main__":
    unittest.main()
