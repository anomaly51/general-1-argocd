"""Compose a feature preview from verified PR heads; all deployment writes go to Git."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "utility-apps/playground-previews/lifecycle/files"))
from lifecycle import APIError, GitDatabase, GitHub  # noqa: E402
from registry_release import Registry  # noqa: E402

SERVICES = ("order-service", "pricing-service", "inventory-service", "event-hub",
            "analytics-service", "shell", "topology-mfe", "traffic-mfe")
UTILITIES = ("namespace", "kafka", "postgres", "mysql", "rabbitmq", "redis", "edge")
REPOSITORY = "anomaly51/general-1-argocd"
REPO_URL = "https://github.com/" + REPOSITORY + ".git"
REGISTRY = "harbor.internal.api-api-api.com"
VAULT = "https://vault.internal.api-api-api.com"
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
INACTIVE = {"expired", "closed", "failed"}


def now_string(now=None):
    return (now or datetime.now(timezone.utc)).isoformat(timespec="seconds").replace("+00:00", "Z")


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def namespace_for(branch):
    if not isinstance(branch, str) or not branch.startswith("feature/") or len(branch) > 180:
        raise ValueError("Preview branches must use feature/<name> (maximum 180 characters)")
    if any(ord(c) < 32 for c in branch) or branch.endswith("/"):
        raise ValueError("Invalid feature branch")
    slug = re.sub(r"[^a-z0-9]+", "-", branch.lower()).strip("-")[:24].rstrip("-")
    return "playground-preview-" + slug + "-" + hashlib.sha256(branch.encode()).hexdigest()[:10]


def current_prs(github, branch):
    result = {}
    for service in SERVICES:
        repo = "anomaly51/playground-" + service
        for page in range(1, 11):
            pulls = github.request(f"/repos/{repo}/pulls?state=open&per_page=100&page={page}")
            for pr in pulls:
                if pr["head"]["ref"] != branch:
                    continue
                if (pr["head"].get("repo", {}).get("full_name") != repo
                        or pr["base"].get("repo", {}).get("full_name") != repo
                        or pr["author_association"] not in {"OWNER", "MEMBER", "COLLABORATOR"}
                        or pr["base"]["ref"] not in {"main", "dev"}):
                    continue  # An unrelated fork must not block a trusted feature group.
                if service in result or not SHA.fullmatch(pr["head"]["sha"]):
                    raise ValueError("Ambiguous PR membership or invalid head SHA")
                result[service] = {"number": pr["number"], "head": pr["head"]["sha"], "repository": repo}
            if len(pulls) < 100:
                break
        else:
            raise ValueError("PR pagination limit exceeded; refusing incomplete membership")
    return result


def successful_ci(github, service, pr):
    repo, head = pr["repository"], pr["head"]
    query = urllib.parse.urlencode({"event": "pull_request", "head_sha": head, "per_page": 100})
    runs = github.request(f"/repos/{repo}/actions/workflows/ci.yaml/runs?{query}")
    for run in runs.get("workflow_runs", []):
        if (run.get("path") == ".github/workflows/ci.yaml" and run.get("event") == "pull_request"
                and run.get("status") == "completed" and run.get("conclusion") == "success"
                and run.get("head_sha") == head
                and run.get("head_repository", {}).get("full_name") == repo):
            # The new PR CI tests/builds the exact head, not a moving merge reference.
            content = github.request(f"/repos/{repo}/contents/.github/workflows/ci.yaml?ref={head}")
            import base64
            workflow = yaml.safe_load(base64.b64decode(content["content"]).decode())
            jobs = workflow.get("jobs", {})
            image_job = "build" if service in {"shell", "topology-mfe", "traffic-mfe"} else "docker-pr"
            for job in ["tests", image_job]:
                checkouts = [step for step in jobs.get(job, {}).get("steps", [])
                             if step.get("uses", "").startswith("actions/checkout@")]
                if len(checkouts) != 1 or checkouts[0].get("with", {}).get("ref") != "${{ github.event.pull_request.head.sha || github.sha }}":
                    raise ValueError("PR must include the preview-capable exact-head CI configuration")
            return run["id"]
    return None


def event_branch(github, event):
    payload = event.get("client_payload", {})
    service, run_id = payload.get("service"), payload.get("run_id")
    if service not in SERVICES or not str(run_id).isdecimal():
        raise ValueError("Invalid preview notification")
    repo = "anomaly51/playground-" + service
    run = github.request(f"/repos/{repo}/actions/runs/{run_id}")
    if (run.get("path") != ".github/workflows/ci.yaml" or run.get("event") != "pull_request"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("head_repository", {}).get("full_name") != repo):
        raise ValueError("Notification is not a successful trusted PR CI run")
    branch = run.get("head_branch", "")
    namespace_for(branch)
    return branch, service, run["head_sha"]


class Vault:
    def __init__(self):
        request_url = os.environ["ACTIONS_ID_TOKEN_REQUEST_URL"]
        parsed = urllib.parse.urlsplit(request_url)
        if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".actions.githubusercontent.com"):
            raise ValueError("Unexpected GitHub OIDC endpoint")
        url = request_url + ("&" if "?" in request_url else "?") + "audience=playground-preview"
        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + os.environ["ACTIONS_ID_TOKEN_REQUEST_TOKEN"]})
        with urllib.request.urlopen(req, timeout=20) as response:
            jwt = json.load(response)["value"]
        self.token = self.request("auth/github/login", "POST", {
            "role": "ci-playground-preview", "jwt": jwt}, authenticated=False)["auth"]["client_token"]

    def request(self, path, method="GET", data=None, authenticated=True, missing=False):
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Vault-Token"] = self.token
        req = urllib.request.Request(VAULT + "/v1/" + path, method=method,
                                     headers=headers, data=None if data is None else json.dumps(data).encode())
        try:
            with urllib.request.urlopen(req, timeout=20) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if missing and error.code == 404:
                return None
            raise RuntimeError(f"Scoped Vault operation failed: HTTP {error.code}") from None
        return json.loads(raw) if raw else None

    def get(self, path):
        return self.request("kv/data/" + path)["data"]["data"]

    def runtime(self, namespace):
        path = "apps/" + namespace
        existing = self.request("kv/data/" + path, missing=True)
        if existing is None:
            data = {k: secrets.token_urlsafe(32) for k in ["POSTGRES_PASSWORD", "MYSQL_PASSWORD",
                    "MYSQL_ROOT_PASSWORD", "REDIS_PASSWORD", "RABBITMQ_PASSWORD", "RABBITMQ_ADMIN_PASSWORD"]}
            data.update(POSTGRES_USERNAME="playground", POSTGRES_DATABASE="playground",
                        MYSQL_USER="playground", MYSQL_DATABASE="playground",
                        RABBITMQ_USERNAME="playground", RABBITMQ_ADMIN_USERNAME="playground-admin")
            data["POSTGRES_URL"] = f"postgresql://playground:{data['POSTGRES_PASSWORD']}@playground-postgres-rw:5432/playground"
            data["FLASHDROP_ANALYTICS_POSTGRES_URL"] = data["POSTGRES_URL"]
            data["MYSQL_URL"] = f"mysql://playground:{data['MYSQL_PASSWORD']}@mysql:3306/playground"
            data["REDIS_URL"] = f"redis://:{data['REDIS_PASSWORD']}@playground-redis:6379/0"
            data["RABBITMQ_URL"] = f"amqp://playground:{data['RABBITMQ_PASSWORD']}@rabbitmq:5672/"
            self.request("kv/data/" + path, "POST", {"options": {"cas": 0}, "data": data})
        if self.request("kv/data/" + path + "/registry", missing=True) is None:
            self.request("kv/data/" + path + "/registry", "POST", {
                "options": {"cas": 0}, "data": self.get("apps/playground-prod/registry")})
        policy = "\n".join('path "kv/data/' + p + '" { capabilities = ["read"] }'
                           for p in [path, path + "/registry"])
        policy += '\npath "auth/token/renew-self" { capabilities = ["update"] }'
        policy += '\npath "auth/token/lookup-self" { capabilities = ["read"] }'
        self.request("sys/policies/acl/" + namespace, "PUT", {"policy": policy})
        self.request("auth/kubernetes/role/" + namespace, "POST", {
            "bound_service_account_names": ["playground-runtime"],
            "bound_service_account_namespaces": [namespace], "audience": "vault",
            "token_policies": [namespace], "token_ttl": 600, "token_max_ttl": 900,
            "token_no_default_policy": True})


def command(args, cwd=None, stdin=None):
    result = subprocess.run(args, cwd=cwd, input=stdin, text=True, capture_output=True)
    if result.returncode:
        # Build output can contain application configuration: don't dump arbitrary output with credentials.
        raise RuntimeError(f"Command failed: {args[0]} {args[1]} (exit {result.returncode})")
    return result.stdout.strip()


def build_image(service, pr, run_id, vault, previous):
    readonly = vault.get("apps/playground-prod/registry")
    os.environ.update(REGISTRY_USERNAME=readonly["username"], REGISTRY_PASSWORD=readonly["password"])
    repository = REGISTRY + "/playground/" + service
    registry = Registry(repository)
    if previous and previous.get("head") == pr["head"] and DIGEST.fullmatch(previous.get("digest", "")):
        digest, source = registry.image(previous["digest"], pr["head"], require_main_label=False)
        if source != pr["repository"] or digest != previous["digest"]:
            raise ValueError("Cached preview image repository mismatch")
        return {"digest": digest, "head": pr["head"], "run_id": run_id}
    credentials = vault.get("ci/playground/" + service)
    with tempfile.TemporaryDirectory(prefix="playground-preview-build-") as tmp:
        root = Path(tmp)
        source = root / "source"
        command(["git", "clone", "--filter=blob:none", "--no-checkout", "https://github.com/" + pr["repository"] + ".git", str(source)])
        command(["git", "checkout", "--detach", pr["head"]], cwd=source)
        if command(["git", "rev-parse", "HEAD"], cwd=source) != pr["head"]:
            raise ValueError("Checked-out preview revision mismatch")
        command(["docker", "login", REGISTRY, "--username", credentials["username"], "--password-stdin"],
                stdin=credentials["password"])
        try:
            args = ["docker", "buildx", "build", "--platform", "linux/amd64", "--push", "--provenance=false",
                    "--tag", repository + ":preview-" + pr["head"], "--metadata-file", str(root / "metadata.json"),
                    "--label", "org.opencontainers.image.source=https://github.com/" + pr["repository"],
                    "--label", "org.opencontainers.image.revision=" + pr["head"],
                    "--label", "io.gitops.source-branch=preview"]
            if service in {"shell", "topology-mfe", "traffic-mfe"}:
                args += ["--build-arg", "VITE_ENABLE_TOOL_LINKS=false"]
            command(args + [str(source)])
        finally:
            command(["docker", "logout", REGISTRY])
        digest = json.loads((root / "metadata.json").read_text())["containerimage.digest"]
        if not DIGEST.fullmatch(digest):
            raise ValueError("BuildKit returned an invalid image digest")
        verified, origin = registry.image(digest, pr["head"], require_main_label=False)
        if verified != digest or origin != pr["repository"]:
            raise ValueError("Built image provenance mismatch")
        print("Built verified preview image:", service, pr["head"][:12], flush=True)
        return {"digest": digest, "head": pr["head"], "run_id": run_id}


def baseline_snapshot():
    baseline = {}
    for service in SERVICES:
        values = yaml.safe_load((ROOT / f"apps/playground-{service}/values/staging.yaml").read_text())
        if not re.fullmatch(r"staging@sha256:[0-9a-f]{64}", values["image"]["tag"]):
            raise ValueError("Staging baseline must use a pinned staging digest")
        baseline[service] = values
    return baseline


def utility_snapshot():
    return {utility: yaml.safe_load((ROOT / f"utility-apps/playground-staging/{utility}/values.yaml").read_text())
            for utility in UTILITIES}


def sources_for(state, revision):
    if not SHA.fullmatch(revision):
        raise ValueError("Preview charts must be pinned to a published commit")
    sources = []
    for service in SERVICES:
        values = copy.deepcopy(state["baseline"][service])
        values.pop("_release", None)
        values["ephemeral"] = True
        if service in state["images"]:
            digest = state["images"][service]["digest"]
            if not DIGEST.fullmatch(digest):
                raise ValueError("Preview image must have an immutable digest")
            values["image"]["tag"] = "preview@" + digest
        values["nodeSelector"] = {"kubernetes.io/hostname": "general-1-worker-3" if service == "order-service" else "general-1-worker-2"}
        if "CORS_ORIGINS" in values.get("env", {}):
            values["env"]["CORS_ORIGINS"] = state["url"]
        sources.append({"repoURL": REPO_URL, "targetRevision": revision, "path": "apps/playground-" + service,
                        "helm": {"releaseName": service, "valuesObject": values}})
    for utility in UTILITIES:
        values = copy.deepcopy(state["utilities"][utility])
        values["ephemeral"] = True
        if "storage" in values:
            values["storage"]["nodeName"] = "general-1-worker-3" if utility in {"postgres", "redis"} else "general-1-worker-1"
            values["storage"]["size"] = "1Gi" if utility != "kafka" else "2Gi"
        if utility == "namespace":
            values["vault"].update(role=state["namespace"], envPath="apps/" + state["namespace"],
                                    registryPath="apps/" + state["namespace"] + "/registry")
        elif utility == "edge":
            values["hostname"] = state["url"].removeprefix("https://")
            values["resources"]["requests"]["memory"] = "16Mi"
        else:
            values["vaultPath"] = "apps/" + state["namespace"]
            # A low-load, disposable preview, not a production sizing profile.
            requests = {"kafka": "512Mi", "mysql": "192Mi", "rabbitmq": "160Mi", "postgres": "128Mi", "redis": "32Mi"}
            values["resources"]["requests"]["memory"] = requests[utility]
            if utility == "kafka":
                values["topicOperatorResources"]["requests"]["memory"] = "64Mi"
        sources.append({"repoURL": REPO_URL, "targetRevision": revision,
                        "path": "utility-apps/playground-staging/" + utility,
                        "helm": {"releaseName": utility, "valuesObject": values}})
    return sources


def atomic_state(database, namespace, mutate):
    state_path, active_path = f"previews/state/{namespace}.json", f"previews/active/{namespace}.json"
    for attempt in range(8):
        head, tree, paths = database.snapshot()
        state = database.read_json(paths[state_path]) if state_path in paths else None
        active = database.read_json(paths[active_path]) if active_path in paths else None
        new_state, new_active = mutate(state, active)
        changes = {}
        if new_state != state:
            changes[state_path] = new_state
        if new_active != active:
            changes[active_path] = new_active
        if not changes:
            return new_state
        try:
            database.commit(head, tree, changes, {state_path, active_path})
            return new_state
        except APIError as error:
            if error.status not in {409, 422} or attempt == 7:
                raise
            time.sleep(min(attempt + 1, 5))
    raise RuntimeError("Concurrent preview state could not be reconciled")


def prepare_state(state, active, branch, prs, refresh, revision, baseline, now=None, utilities=None):
    now = now or datetime.now(timezone.utc)
    namespace = namespace_for(branch)
    if state and state["phase"] in INACTIVE:
        if not refresh:
            raise ValueError("Preview has expired/closed; use Refresh Preview to create a new lease")
        if not state.get("cleanup_completed_at"):
            raise ValueError("Preview removal is still in progress; retry Refresh after cleanup finishes")
        state, active = None, None
    if state is None:
        state = {"schema": 1, "namespace": namespace, "branch": branch,
                 "url": "https://" + namespace + ".internal.api-api-api.com",
                 "generation": str(uuid.uuid4()), "phase": "starting", "created_at": now_string(now),
                 "startup_deadline": now_string(now + timedelta(minutes=30)), "expires_at": None,
                 "ready_at": None, "prs": prs, "baseline": baseline, "images": {}, "revision": revision,
                 "utilities": utilities if utilities is not None else utility_snapshot()}
    else:
        state = copy.deepcopy(state)
        if state["namespace"] != namespace or state["branch"] != branch:
            raise ValueError("Preview identity mismatch")
        if state.get("expires_at") and timestamp(state["expires_at"]) <= now and not refresh:
            raise ValueError("Preview lease expired; use Refresh Preview")
        if refresh:
            state["generation"] = str(uuid.uuid4())
            if state.get("ready_at"):
                state["expires_at"] = now_string(now + timedelta(minutes=15))
            else:
                state["startup_deadline"] = now_string(now + timedelta(minutes=30))
        if state["prs"] != prs:
            state["phase"] = "starting"
        state["prs"] = prs
    return state, active


def configure_preview(branch, refresh, notification=None):
    namespace = namespace_for(branch)
    github, database = GitHub(os.environ["GH_TOKEN"]), None
    database = GitDatabase(github)
    _, _, paths = database.snapshot()
    config = database.read_json(paths["previews/config.json"])
    if config.get("enabled") is not True:
        raise ValueError("Preview automation is not enabled yet")
    prs = current_prs(github, branch)
    if not prs:
        raise ValueError("There are no open trusted PRs for this feature branch")
    if notification:
        service, head = notification
        if service not in prs or prs[service]["head"] != head:
            raise ValueError("Ignoring a notification from a stale or closed PR")
    baseline = baseline_snapshot()
    state = atomic_state(database, namespace, lambda old, active: prepare_state(
        old, active, branch, prs, refresh, config["chart_revision"], baseline))
    generation = state["generation"]
    runs = {service: successful_ci(github, service, pr) for service, pr in prs.items()}
    pending = [service for service, run in runs.items() if run is None]
    if pending:
        print("Preview is pending; latest PR CI must pass for:", ", ".join(pending))
        return state, False
    def provisioning(latest, active, start):
        if not latest or latest["generation"] != generation:
            raise ValueError("Preview generation changed before secret provisioning")
        latest = copy.deepcopy(latest)
        if start:
            now = datetime.now(timezone.utc)
            if latest["phase"] in INACTIVE or latest.get("cleanup_started_at") or (
                    latest.get("expires_at") and timestamp(latest["expires_at"]) <= now):
                raise ValueError("Preview expired before secret provisioning")
            latest["provisioning_until"] = now_string(now + timedelta(minutes=5))
        else:
            latest.pop("provisioning_until", None)
        return latest, active

    atomic_state(database, namespace, lambda latest, active: provisioning(latest, active, True))
    try:
        vault = Vault()
        vault.runtime(namespace)
    finally:
        atomic_state(database, namespace, lambda latest, active: provisioning(latest, active, False))
    images = {service: image for service, image in state["images"].items() if service in prs}
    for service, pr in prs.items():
        images[service] = build_image(service, pr, runs[service], vault, images.get(service))
    if current_prs(github, branch) != prs:
        raise ValueError("PR membership/head changed during build; await the latest CI notification")

    def publish(latest, active):
        if (not latest or latest["generation"] != generation or latest["phase"] in INACTIVE
                or latest.get("cleanup_started_at")):
            raise ValueError("Preview generation changed/expired during build; refusing resurrection")
        if latest.get("expires_at") and timestamp(latest["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("Preview expired during build; use Refresh Preview")
        if not latest.get("ready_at") and timestamp(latest["startup_deadline"]) <= datetime.now(timezone.utc):
            raise ValueError("Preview startup deadline exceeded during build; use Refresh after cleanup")
        latest = copy.deepcopy(latest)
        latest.update(images=images, prs=prs, phase="starting")
        sources = sources_for(latest, latest["revision"])
        latest["plan_hash"] = hashlib.sha256(json.dumps(sources, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        active = {"name": namespace, "namespace": namespace, "branch": branch, "url": latest["url"], "sources": sources}
        return latest, active

    state = atomic_state(database, namespace, publish)
    print("Preview desired state committed:", state["url"], flush=True)
    return state, True


def wait_ready(namespace, generation):
    database = GitDatabase(GitHub(os.environ["GH_TOKEN"]))
    path = f"previews/state/{namespace}.json"
    deadline = time.monotonic() + 1200
    while time.monotonic() < deadline:
        _, _, paths = database.snapshot()
        state = database.read_json(paths[path])
        if state["generation"] != generation or state["phase"] in INACTIVE:
            raise ValueError("Preview was superseded, expired or failed while waiting")
        if state["phase"] == "ready":
            print("Preview READY:", state["url"], "expires:", state["expires_at"], flush=True)
            return state
        print("Waiting for all preview components to be Synced/Healthy", flush=True)
        time.sleep(20)
    raise TimeoutError("Preview is still pending; inspect Argo CD and available cluster resources")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--operation", choices=["reconcile", "refresh"], default="reconcile")
    parser.add_argument("--branch", default="")
    parser.add_argument("--wait-namespace")
    parser.add_argument("--generation")
    args = parser.parse_args()
    if (os.environ.get("GITHUB_REPOSITORY") != REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") not in {"repository_dispatch", "workflow_dispatch"}):
        raise ValueError("Preview orchestration must execute from the trusted GitOps main workflow")
    if args.wait_namespace:
        if not re.fullmatch(r"playground-preview-[a-z0-9-]{1,24}-[0-9a-f]{10}", args.wait_namespace) or not args.generation:
            raise ValueError("Invalid preview readiness identity")
        state = wait_ready(args.wait_namespace, args.generation)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as stream:
                stream.write(f"## Preview ready\n\n{state['url']}\n\nExpires: `{state['expires_at']}`\n")
        return
    notification = None
    branch = args.branch
    if os.environ["GITHUB_EVENT_NAME"] == "repository_dispatch":
        event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
        branch, service, head = event_branch(GitHub(os.environ["GH_TOKEN"]), event)
        notification = service, head
    state, deployed = configure_preview(branch, args.operation == "refresh", notification)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a") as stream:
            stream.write(f"deployed={str(deployed).lower()}\nnamespace={state['namespace']}\ngeneration={state['generation']}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as stream:
            stream.write(f"## Preview {'deploying' if deployed else 'pending'}\n\n{state['url']}\n\n"
                         f"Branch: `{branch}`\n\nTTL: 15 minutes from readiness; refresh in this workflow.\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        # Never print request headers, Vault responses, environment variables or arbitrary subprocess output.
        print(type(error).__name__ + ": " + str(error), file=sys.stderr)
        raise SystemExit(1)
