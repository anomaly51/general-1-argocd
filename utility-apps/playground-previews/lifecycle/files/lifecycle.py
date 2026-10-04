#!/usr/bin/env python3
"""Reconcile preview leases by committing desired state, never mutating workloads."""

import base64
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request


API_ORIGIN = "https://api.github.com"
REPOSITORY = "anomaly51/general-1-argocd"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
SERVICES = frozenset({"order-service", "pricing-service", "inventory-service", "event-hub",
                      "analytics-service", "shell", "topology-mfe", "traffic-mfe"})
TERMINAL_PHASES = frozenset({"expired", "closed", "failed"})
STATE_PREFIX = "previews/state/"
ACTIVE_PREFIX = "previews/active/"
KUBERNETES_ORIGIN = "https://kubernetes.default.svc"
VAULT_ORIGIN = "http://vault.vault-system.svc.cluster.local:8200"


class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"GitHub API request failed (HTTP {status})")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError("Unexpected API redirect rejected")


def compact_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def base64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def app_jwt(app_id, private_key_path, now=None):
    if not re.fullmatch(r"[0-9]+", str(app_id)):
        raise ValueError("Invalid GitHub App identifier")
    now = int(time.time() if now is None else now)
    header = base64url(compact_json({"alg": "RS256", "typ": "JWT"}).encode())
    payload = base64url(compact_json({"iat": now - 60, "exp": now + 540,
                                      "iss": str(app_id)}).encode())
    signing_input = f"{header}.{payload}".encode("ascii")
    result = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(private_key_path)],
        input=signing_input, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        check=False, timeout=10,
    )
    if result.returncode or not result.stdout:
        raise RuntimeError("Unable to sign GitHub App authentication")
    return f"{header}.{payload}.{base64url(result.stdout)}"


class GitHub:
    def __init__(self, token):
        self.token = token
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, method="GET", data=None):
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ValueError("Invalid GitHub API path")
        url = API_ORIGIN + path
        request = urllib.request.Request(
            url, data=None if data is None else compact_json(data).encode(),
            method=method,
            headers={"Authorization": f"Bearer {self.token}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json",
                     "User-Agent": "playground-preview-lifecycle"},
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Do not expose response bodies, signed links or credentials in logs.
            raise APIError(exc.code) from None
        except urllib.error.URLError:
            raise RuntimeError("GitHub API connection failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("GitHub API response exceeds size limit")
        return json.loads(raw) if raw else None


def installation_client(app_id_path, private_key_path):
    app_id = Path(app_id_path).read_text().strip()
    app = GitHub(app_jwt(app_id, private_key_path))
    installation = app.request(f"/repos/{REPOSITORY}/installation")
    installation_id = installation.get("id")
    if not isinstance(installation_id, int) or installation_id < 1:
        raise ValueError("Invalid GitHub App installation")
    token = app.request(f"/app/installations/{installation_id}/access_tokens", "POST", {
        "repositories": ["general-1-argocd"], "permissions": {"contents": "write"},
    }).get("token")
    if not isinstance(token, str) or not token:
        raise ValueError("Missing GitHub installation token")
    return GitHub(token)


class GitDatabase:
    """Git tree changes with a non-forced ref update; callers retry from a fresh tree."""

    def __init__(self, github, branch="main"):
        if branch != "main":
            raise ValueError("Lifecycle may only reconcile the configured main branch")
        self.github = github
        self.prefix = f"/repos/{REPOSITORY}"
        self.branch = branch

    def snapshot(self):
        head = self.github.request(f"{self.prefix}/git/ref/heads/{self.branch}")["object"]["sha"]
        commit = self.github.request(f"{self.prefix}/git/commits/{head}")
        tree_sha = commit["tree"]["sha"]
        tree = self.github.request(f"{self.prefix}/git/trees/{tree_sha}?recursive=1")
        if tree.get("truncated"):
            raise ValueError("Repository tree was truncated; refusing partial reconciliation")
        paths = {entry["path"]: entry for entry in tree["tree"] if entry["type"] == "blob"}
        return head, tree_sha, paths

    def read_json(self, entry):
        if entry.get("mode") != "100644":
            raise ValueError("Preview state must be a regular non-executable file")
        blob = self.github.request(f"{self.prefix}/git/blobs/{entry['sha']}")
        if blob.get("encoding") != "base64" or blob.get("size", 0) > 1024 * 1024:
            raise ValueError("Invalid preview state blob")
        raw = base64.b64decode(blob["content"].replace("\n", ""), validate=True)
        if len(raw) > 1024 * 1024:
            raise ValueError("Preview state exceeds size limit")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Preview state must contain a JSON object")
        return result

    def commit(self, head, tree_sha, changes, allowed_paths, message="chore(preview): reconcile leases"):
        if not changes:
            return False
        if not set(changes).issubset(allowed_paths):
            raise ValueError("Lifecycle attempted to write outside validated preview files")
        entries = []
        for path, content in sorted(changes.items()):
            entry = {"path": path, "mode": "100644", "type": "blob"}
            if content is None:
                entry["sha"] = None
            else:
                entry["content"] = json.dumps(content, indent=2, sort_keys=True) + "\n"
            entries.append(entry)
        tree = self.github.request(f"{self.prefix}/git/trees", "POST", {
            "base_tree": tree_sha, "tree": entries,
        })
        commit = self.github.request(f"{self.prefix}/git/commits", "POST", {
            "message": message, "tree": tree["sha"], "parents": [head],
        })
        self.github.request(f"{self.prefix}/git/refs/heads/{self.branch}", "PATCH", {
            "sha": commit["sha"], "force": False,
        })
        return True


def utc_now():
    return datetime.now(timezone.utc)


def format_date(value):
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_date(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("Preview timestamps must be UTC RFC3339 values")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid preview timestamp") from None


def preview_namespace(branch):
    if not isinstance(branch, str) or not branch or len(branch) > 255 or any(ord(c) < 32 for c in branch):
        raise ValueError("Invalid preview branch")
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:24].rstrip("-")
    if not slug:
        raise ValueError("Preview branch must contain an alphanumeric character")
    suffix = hashlib.sha256(branch.encode()).hexdigest()[:10]
    return f"playground-preview-{slug}-{suffix}"


def validate_state(path, state):
    namespace = preview_namespace(state.get("branch"))
    if (state.get("schema") != 1 or state.get("namespace") != namespace
            or path != f"{STATE_PREFIX}{namespace}.json"):
        raise ValueError("Preview state path, branch and namespace do not agree")
    if state.get("phase") not in {"starting", "ready", *TERMINAL_PHASES}:
        raise ValueError("Invalid preview phase")
    if not isinstance(state.get("generation"), str) or not state["generation"]:
        raise ValueError("Preview state has no generation")
    parse_date(state.get("created_at"))
    parse_date(state.get("startup_deadline"))
    if state.get("expires_at"):
        parse_date(state["expires_at"])
    if state.get("ready_at"):
        parse_date(state["ready_at"])
    if state.get("provisioning_until"):
        parse_date(state["provisioning_until"])
    if state["phase"] == "ready":
        parse_date(state.get("ready_at"))
        parse_date(state.get("expires_at"))
    prs = state.get("prs")
    if not isinstance(prs, dict) or not prs or not set(prs).issubset(SERVICES):
        raise ValueError("Invalid preview pull request set")
    for service, pr in prs.items():
        if (not isinstance(pr, dict) or pr.get("repository") != f"anomaly51/playground-{service}"
                or type(pr.get("number")) is not int or pr["number"] < 1
                or not re.fullmatch(r"[0-9a-f]{40}", str(pr.get("head", "")))):
            raise ValueError("Invalid preview pull request identity")
    return namespace


def validate_active(state, active):
    namespace = state["namespace"]
    if (not isinstance(active, dict) or active.get("name") != namespace
            or active.get("namespace") != namespace or active.get("branch") != state["branch"]
            or not isinstance(active.get("sources"), list) or not active["sources"]):
        raise ValueError("Active preview does not match its lease")
    plan_hash = hashlib.sha256(compact_json(active["sources"]).encode()).hexdigest()
    if state.get("plan_hash") != plan_hash:
        raise ValueError("Active preview sources do not match their recorded plan")


def application_ready(application, active):
    if not application:
        return False
    status = application.get("status", {})
    sync = status.get("sync", {})
    return (application.get("spec", {}).get("sources") == active["sources"]
            and sync.get("comparedTo", {}).get("sources") == active["sources"]
            and sync.get("status") == "Synced"
            and status.get("health", {}).get("status") == "Healthy"
            and status.get("operationState", {}).get("phase") != "Running"
            and not application.get("operation"))


def inspect_prs(github, state):
    closed = set()
    current_heads_match = True
    for service, expected in sorted(state["prs"].items()):
        pr = github.request(f"/repos/{expected['repository']}/pulls/{expected['number']}")
        if (pr.get("number") != expected["number"]
                or pr.get("base", {}).get("repo", {}).get("full_name") != expected["repository"]):
            raise ValueError("GitHub pull request identity mismatch")
        if pr.get("state") == "open":
            current_heads_match = current_heads_match and (
                pr.get("head", {}).get("repo", {}).get("full_name") == expected["repository"]
                and pr.get("head", {}).get("ref") == state["branch"]
                and pr.get("head", {}).get("sha") == expected["head"])
            continue
        if pr.get("state") != "closed":
            raise ValueError("Unexpected GitHub pull request state")
        closed.add(service)
    return closed, current_heads_match


def open_branch_prs(github, state):
    # CI completion notifications arrive too late to notice a newly opened PR
    # whose checks are still pending or failing. Scan the complete eligible
    # feature group before declaring its previously published plan ready.
    head = urllib.parse.quote("anomaly51:" + state["branch"], safe="")
    result = {}
    for service in sorted(SERVICES):
        repository = f"anomaly51/playground-{service}"
        for page in range(1, 11):
            prs = github.request(f"/repos/{repository}/pulls?state=open&head={head}&per_page=100&page={page}")
            if not isinstance(prs, list):
                raise ValueError("Unexpected GitHub pull request listing")
            for pr in prs:
                pr_head, base = pr.get("head") or {}, pr.get("base") or {}
                if (pr.get("state") != "open" or pr_head.get("ref") != state["branch"]
                        or (pr_head.get("repo") or {}).get("full_name") != repository
                        or (base.get("repo") or {}).get("full_name") != repository
                        or base.get("ref") not in {"main", "dev"}
                        or pr.get("author_association") not in {"OWNER", "MEMBER", "COLLABORATOR"}):
                    continue
                if (service in result or type(pr.get("number")) is not int or pr["number"] < 1
                        or not re.fullmatch(r"[0-9a-f]{40}", str(pr_head.get("sha", "")))):
                    raise ValueError("Ambiguous or invalid live preview PR membership")
                result[service] = {"number": pr["number"], "head": pr_head["sha"], "repository": repository}
            if len(prs) < 100:
                break
        else:
            raise ValueError("Preview PR pagination limit exceeded")
    return result


def restore_closed_components(state, active, services):
    """Closed components return to the group's original staging image, not today's staging."""
    active = copy.deepcopy(active)
    for service in services:
        state["prs"].pop(service)
        state.get("images", {}).pop(service, None)
        if active:
            matches = [source for source in active["sources"]
                       if source.get("path") == f"apps/playground-{service}"]
            if len(matches) != 1 or "valuesObject" not in matches[0].get("helm", {}):
                raise ValueError("Closed component does not have exactly one Helm source")
            baseline_image = state.get("baseline", {}).get(service, {}).get("image")
            if not isinstance(baseline_image, dict):
                raise ValueError("Closed component has no frozen baseline image")
            matches[0]["helm"]["valuesObject"]["image"] = copy.deepcopy(baseline_image)
    state["phase"] = "starting"
    if active:
        state["plan_hash"] = hashlib.sha256(compact_json(active["sources"]).encode()).hexdigest()
    return active


def images_match_pr_heads(state):
    images = state.get("images", {})
    return (set(images) == set(state["prs"])
            and all(images[service].get("head") == pr["head"]
                    and re.fullmatch(r"sha256:[0-9a-f]{64}", str(images[service].get("digest", "")))
                    for service, pr in state["prs"].items()))


def provisioning_finished(state, now):
    return not state.get("provisioning_until") or parse_date(state["provisioning_until"]) <= now


def http_ready(state):
    namespace = preview_namespace(state["branch"])
    expected_url = f"https://{namespace}.internal.api-api-api.com"
    if state.get("namespace") != namespace or state.get("url") != expected_url:
        raise ValueError("Preview readiness URL does not match its namespace")
    opener = urllib.request.build_opener(NoRedirect())
    checks = {"/api/readyz": ("processor", "postgres", "redis", "brokers"),
              "/events/readyz": ("kafka", "rabbitmq")}
    for path, required in checks.items():
        try:
            request = urllib.request.Request(expected_url + path,
                                             headers={"Accept": "application/json"})
            with opener.open(request, timeout=5) as response:
                if response.status != 200:
                    return False
                raw = response.read(65537)
            if len(raw) > 65536:
                return False
            body = json.loads(raw)
            if not isinstance(body, dict) or body.get("status") != "ready":
                return False
            dependencies = body.get("dependencies", {})
            if not isinstance(dependencies, dict) or any(dependencies.get(key) is not True for key in required):
                return False
        except (urllib.error.URLError, TimeoutError, RuntimeError, ValueError, OSError):
            return False
    return True


class Kubernetes:
    """Discover workloads; only cancel an already-retired Argo operation's status."""

    def __init__(self, token_path="/kubernetes/token", ca_path="/kubernetes/ca.crt"):
        self.token_path = token_path
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=ca_path)))

    def _get(self, path):
        token = Path(self.token_path).read_text().strip()
        request = urllib.request.Request(KUBERNETES_ORIGIN + path,
                                         headers={"Authorization": f"Bearer {token}"})
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise RuntimeError(f"Kubernetes read failed (HTTP {exc.code})") from None
        except urllib.error.URLError:
            raise RuntimeError("Kubernetes read failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Kubernetes response exceeds size limit")
        return json.loads(raw)

    def application(self, namespace):
        if not re.fullmatch(r"playground-preview-[a-z0-9-]{1,24}-[0-9a-f]{10}", namespace):
            raise ValueError("Invalid preview application name")
        return self._get(f"/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{namespace}")

    def namespace_exists(self, namespace):
        if not re.fullmatch(r"playground-preview-[a-z0-9-]{1,24}-[0-9a-f]{10}", namespace):
            raise ValueError("Invalid preview namespace")
        return self._get(f"/api/v1/namespaces/{namespace}") is not None

    def terminate_retired_operation(self, state):
        """Unblock Argo cascading deletion without changing any desired workload.

        Argo CD's TerminateOperation sets operationState.phase on the main
        Application resource (its CRD has no status subresource). Per-preview
        RBAC permits this patch for this application's exact name only.
        """
        namespace = validate_state(f"{STATE_PREFIX}{state['namespace']}.json", state)
        if state["phase"] not in TERMINAL_PHASES or state.get("cleanup_completed_at"):
            return False
        application = self.application(namespace)
        if not application:
            return False
        metadata = application.get("metadata", {})
        if (metadata.get("name") != namespace or metadata.get("namespace") != "argocd"
                or metadata.get("labels", {}).get("gitops.api-api-api.com/environment") != "preview"
                or application.get("spec", {}).get("destination", {}).get("namespace") != namespace):
            raise ValueError("Retired application identity does not match preview")
        if (not metadata.get("deletionTimestamp") or not application.get("operation")
                or application.get("status", {}).get("operationState", {}).get("phase") != "Running"):
            return False
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise ValueError("Retired application lacks concurrency identity")
        operations = [
            {"op": "test", "path": "/metadata/uid", "value": metadata["uid"]},
            {"op": "test", "path": "/metadata/resourceVersion", "value": metadata["resourceVersion"]},
            {"op": "test", "path": "/status/operationState/phase", "value": "Running"},
            {"op": "replace", "path": "/status/operationState/phase", "value": "Terminating"},
        ]
        token = Path(self.token_path).read_text().strip()
        request = urllib.request.Request(
            f"{KUBERNETES_ORIGIN}/apis/argoproj.io/v1alpha1/namespaces/argocd/applications/{namespace}",
            data=compact_json(operations).encode(), method="PATCH",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json-patch+json"})
        try:
            with self.opener.open(request, timeout=15):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 409, 422):
                # Gone or changed after our GET: a fresh Cron invocation must
                # re-read Git state and the resource before another attempt.
                return False
            raise RuntimeError(f"Argo operation termination failed (HTTP {exc.code})") from None
        except urllib.error.URLError:
            raise RuntimeError("Argo operation termination connection failed") from None
        return True


class Vault:
    def __init__(self, jwt_path="/vault/token"):
        self.jwt_path = jwt_path
        self.token = None
        self.opener = urllib.request.build_opener(NoRedirect())

    def _request(self, path, method="POST", data=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Vault-Token"] = self.token
        request = urllib.request.Request(VAULT_ORIGIN + "/v1/" + path,
                                         data=None if data is None else compact_json(data).encode(),
                                         method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                raw = response.read(1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            if method == "DELETE" and exc.code == 404:
                return None
            raise RuntimeError(f"Vault lifecycle operation failed (HTTP {exc.code})") from None
        except urllib.error.URLError:
            raise RuntimeError("Vault lifecycle connection failed") from None
        if len(raw) > 1024 * 1024:
            raise ValueError("Vault response exceeds size limit")
        return json.loads(raw) if raw else None

    def cleanup(self, state):
        namespace = validate_state(f"{STATE_PREFIX}{state['namespace']}.json", state)
        if (state["phase"] not in TERMINAL_PHASES or not state.get("cleanup_started_at")
                or state.get("cleanup_completed_at")):
            raise ValueError("Vault cleanup requires a claimed inactive preview")
        if self.token is None:
            response = self._request("auth/kubernetes/login", data={
                "role": "playground-preview-lifecycle", "jwt": Path(self.jwt_path).read_text().strip(),
            })
            self.token = response["auth"]["client_token"]
        for path in (f"kv/metadata/apps/{namespace}/registry", f"kv/metadata/apps/{namespace}",
                     f"auth/kubernetes/role/{namespace}", f"sys/policies/acl/{namespace}"):
            self._request(path, "DELETE")


def load_previews(database, paths):
    states, actives = {}, {}
    for path, entry in sorted(paths.items()):
        if not path.startswith(STATE_PREFIX) or not path.endswith(".json"):
            continue
        state = database.read_json(entry)
        namespace = validate_state(path, state)
        states[path] = state
        active_path = f"{ACTIVE_PREFIX}{namespace}.json"
        if active_path in paths:
            active = database.read_json(paths[active_path])
            validate_active(state, active)
            actives[active_path] = active
    return states, actives


def plan_changes(states, actives, github, kubernetes, now, check_http=http_ready):
    """A fresh snapshot is mandatory on every CAS retry, including lease checks."""
    changes = {}
    for path, previous in sorted(states.items()):
        state = copy.deepcopy(previous)
        active_path = f"{ACTIVE_PREFIX}{state['namespace']}.json"
        active = actives.get(active_path)
        if state["phase"] in TERMINAL_PHASES:
            if active:
                changes[active_path] = None
            if (not active and not state.get("cleanup_completed_at") and not state.get("cleanup_started_at")
                    and provisioning_finished(state, now)
                    and not kubernetes.namespace_exists(state["namespace"])
                    and kubernetes.application(state["namespace"]) is None):
                state["cleanup_started_at"] = format_date(now)
        else:
            reason = None
            membership_pending = False
            current_heads_match = True
            # Updates can be starting while an earlier usable generation's
            # lease is running. Only explicit Refresh may extend that lease.
            if state.get("expires_at") and parse_date(state["expires_at"]) <= now:
                reason = "expired"
            elif (state["phase"] == "starting" and not state.get("ready_at")
                  and parse_date(state["startup_deadline"]) <= now):
                reason = "failed"
            else:
                try:
                    closed, current_heads_match = inspect_prs(github, state)
                    live_prs = open_branch_prs(github, state)
                except (APIError, RuntimeError, ValueError, KeyError, TypeError):
                    # One ambiguous group or failed API read must not prevent
                    # unrelated expired previews from being removed. Never
                    # mistake an unavailable membership list for a closed PR.
                    state["phase"] = "starting"
                    state["membership_error"] = "Pull request membership could not be verified"
                    membership_pending = True
                    current_heads_match = False
                    print("Preview membership check unavailable:", state["namespace"], flush=True)
                else:
                    state.pop("membership_error", None)
                    if not current_heads_match:
                        # A push can race the central workflow's final membership
                        # check. Keep the existing deployment, but do not call it
                        # ready for the new PR head or renew its lease.
                        state["phase"] = "starting"
                    if len(closed) == len(state["prs"]):
                        if not live_prs:
                            reason = "closed"
                        else:
                            membership_pending = True
                    elif closed:
                        active = restore_closed_components(state, active, closed)
                        if active:
                            changes[active_path] = active
                    if live_prs != state["prs"]:
                        membership_pending = True
                    if membership_pending:
                        # Preserve the existing deployment and original lease;
                        # the central builder alone adds verified PR images.
                        state["phase"] = "starting"
            if reason:
                state["phase"] = reason
                state["terminated_at"] = format_date(now)
                if active:
                    changes[active_path] = None
            elif (not membership_pending and current_heads_match and state["phase"] == "starting"
                  and active and images_match_pr_heads(state)
                  and application_ready(kubernetes.application(state["namespace"]), active)
                  and check_http(state)):
                state["phase"] = "ready"
                if not state.get("ready_at"):
                    state.update(ready_at=format_date(now),
                                 expires_at=format_date(now + timedelta(seconds=900)))
        if state != previous:
            changes[path] = state
    return changes


def reconcile(database, github, kubernetes, vault, now=None):
    for attempt in range(4):
        moment = now or utc_now()
        head, tree_sha, paths = database.snapshot()
        states, actives = load_previews(database, paths)
        allowed = set(states) | {f"{ACTIVE_PREFIX}{state['namespace']}.json" for state in states.values()}
        for previous in states.values():
            if (previous["phase"] in TERMINAL_PHASES and not previous.get("cleanup_completed_at")
                    and f"{ACTIVE_PREFIX}{previous['namespace']}.json" not in actives):
                # Only after desired removal is durable in Git and Argo has
                # begun deletion may a stuck Running operation be terminated.
                kubernetes.terminate_retired_operation(previous)
        changes = plan_changes(states, actives, github, kubernetes, moment)
        for path, previous in sorted(states.items()):
            if (previous["phase"] in TERMINAL_PHASES and previous.get("cleanup_started_at")
                    and not previous.get("cleanup_completed_at")
                    and provisioning_finished(previous, moment)
                    and f"{ACTIVE_PREFIX}{previous['namespace']}.json" not in actives
                    and not kubernetes.namespace_exists(previous["namespace"])
                    and kubernetes.application(previous["namespace"]) is None):
                # Use the durable snapshot, never a claim just planned above.
                # Other previews changing must not starve credential cleanup.
                # Reactivation waits for cleanup_completed_at, protecting any
                # refreshed generation while these idempotent deletes run.
                vault.cleanup(previous)
                state = copy.deepcopy(previous)
                state["cleanup_completed_at"] = format_date(moment)
                changes[path] = state
        if not changes:
            return False
        try:
            database.commit(head, tree_sha, changes, allowed)
            print(f"Reconciled {len(changes)} preview state files", flush=True)
            return True
        except APIError as exc:
            if exc.status not in (409, 422) or attempt == 3:
                raise
            # The main branch advanced, potentially by Refresh. Re-read the
            # whole state and recompute: never replay an old expiry decision.
            time.sleep(0.5 * (attempt + 1))
    return False


def main():
    if os.environ.get("GITOPS_REPOSITORY", REPOSITORY) != REPOSITORY:
        raise ValueError("Lifecycle repository is fixed")
    github = installation_client(os.environ.get("GITHUB_APP_ID_FILE", "/github-app/app_id"),
                                 os.environ.get("GITHUB_APP_KEY_FILE", "/github-app/private_key"))
    database = GitDatabase(github, os.environ.get("GITOPS_BRANCH", "main"))
    reconcile(database, github, Kubernetes(), Vault())


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Exceptions are sanitized at boundaries; never dump HTTP response data.
        print(f"Preview lifecycle failed: {type(exc).__name__}: {exc}", flush=True)
        raise SystemExit(1) from None
