"""Promote one healthy playground staging digest; never build or move registry tags."""
from __future__ import annotations

import argparse
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.parse
import urllib.request
import urllib.error
import zipfile

import yaml

from argocd_release import wait
from promote_release import validate_chart
from registry_release import Registry

SERVICES = {"order-service", "pricing-service", "inventory-service", "event-hub",
            "analytics-service", "shell", "topology-mfe", "traffic-mfe"}
SHA = re.compile(r"[0-9a-f]{40}")
STAGED_IMAGE = re.compile(r"staging@(sha256:[0-9a-f]{64})")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RELEASE_BYTES = 64 * 1024
MAX_RELEASE_ARTIFACTS = 20


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, url):
        return None


def github_bytes(path: str, *, archive: bool = False) -> bytes:
    """Keep the GitHub bearer on api.github.com, never on signed storage URLs."""
    url = "https://api.github.com" + path
    headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"],
               "Accept": "application/vnd.github+json"}
    for attempt in range(4):
        request = urllib.request.Request(url, headers=headers)
        try:
            opener = urllib.request.build_opener(NoRedirect())
            with opener.open(request, timeout=30) as response:
                length = response.headers.get("Content-Length")
                if length is not None and (not length.isdecimal() or int(length) > MAX_RESPONSE_BYTES):
                    raise ValueError("GitHub response exceeds the size limit")
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("GitHub response exceeds the size limit")
                return body
        except urllib.error.HTTPError as error:
            code, location = error.code, error.headers.get("Location", "")
            error.close()
            if not archive or code not in {301, 302, 303, 307, 308} or attempt == 3:
                raise RuntimeError(f"GitHub release request failed: HTTP {code}") from None
            try:
                target = urllib.parse.urlsplit(location)
                host, port = target.hostname or "", target.port
            except ValueError:
                raise ValueError("Invalid GitHub artifact download location") from None
            if (target.scheme != "https" or target.username or target.password
                    or port not in {None, 443}
                    or not (host.endswith(".blob.core.windows.net")
                            or host.endswith(".actions.githubusercontent.com")
                            or host == "objects.githubusercontent.com")):
                raise ValueError("Unexpected GitHub artifact download host") from None
            url, headers = location, {}  # Never forward Authorization, even on subsequent redirects.
        except (urllib.error.URLError, TimeoutError, OSError):
            raise RuntimeError("GitHub release request failed; transport details withheld") from None
    raise RuntimeError("GitHub artifact redirect limit exceeded")


def github_json(path: str) -> dict:
    try:
        value = json.loads(github_bytes(path))
    except (ValueError, UnicodeError):
        raise ValueError("Invalid or oversized GitHub release response") from None
    if not isinstance(value, dict):
        raise ValueError("Invalid GitHub release response")
    return value


def release_document(body: bytes) -> dict:
    """Read one small JSON document; no archive member ever reaches the filesystem."""
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError("Release artifact exceeds the size limit")
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            members = archive.infolist()
            if len(members) != 1 or members[0].filename != "release-image.json":
                raise ValueError("Release artifact must contain only release-image.json")
            member = members[0]
            if (member.file_size > MAX_RELEASE_BYTES or member.flag_bits & 1
                    or member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
                    or member.file_size > max(member.compress_size, 1) * 100):
                raise ValueError("Invalid release artifact size or compression")
            with archive.open(member) as stream:
                raw = stream.read(MAX_RELEASE_BYTES + 1)
            if len(raw) > MAX_RELEASE_BYTES:
                raise ValueError("Release document exceeds the size limit")
            document = json.loads(raw)
    except (zipfile.BadZipFile, RuntimeError, ValueError, UnicodeError):
        raise ValueError("Invalid or oversized release-image artifact") from None
    fields = {"service", "repository", "source_repository", "source_commit", "source_branch", "digest"}
    if not isinstance(document, dict) or set(document) != fields or not all(
            isinstance(value, str) for value in document.values()):
        raise ValueError("Invalid release-image fields")
    return document


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def validate_caller(service: str) -> str:
    if service not in SERVICES:
        raise ValueError("Unknown playground service")
    repository = "anomaly51/playground-" + service
    if (os.environ.get("GITHUB_REPOSITORY") != repository
            or os.environ.get("GITHUB_REF") != "refs/heads/main"
            or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"):
        raise ValueError("Promotion must be manually started from this service's main branch")
    return repository


def image_manifest(registry: Registry, digest: str) -> tuple[dict, str]:
    if not DIGEST.fullmatch(digest):
        raise ValueError("Invalid image digest")
    manifest, actual = registry.manifest(digest)
    if actual != digest:
        raise ValueError("Staging manifest digest changed")
    if "manifests" in manifest:
        platforms = [item for item in manifest["manifests"]
                     if item.get("platform", {}).get("os") == "linux"
                     and item.get("platform", {}).get("architecture") == "amd64"]
        if len(platforms) != 1 or not DIGEST.fullmatch(platforms[0].get("digest", "")):
            raise ValueError("Expected exactly one linux/amd64 image manifest")
        digest = platforms[0]["digest"]
        manifest, actual = registry.manifest(digest)
        if actual != digest:
            raise ValueError("Image platform manifest digest mismatch")
    return manifest, digest


def source_revision(registry: Registry, digest: str, repository: str) -> str:
    manifest, _ = image_manifest(registry, digest)
    body, _ = registry.get("blobs/" + manifest["config"]["digest"])
    if "sha256:" + hashlib.sha256(body).hexdigest() != manifest["config"]["digest"]:
        raise ValueError("Image configuration digest mismatch")
    labels = json.loads(body).get("config", {}).get("Labels", {})
    revision = labels.get("org.opencontainers.image.revision", "")
    if (not SHA.fullmatch(revision)
            or labels.get("io.gitops.source-branch") != "main"
            or labels.get("org.opencontainers.image.source") != "https://github.com/" + repository):
        raise ValueError("Only an image built by this repository's main CI may be promoted")
    return revision


def successful_build(repository: str, revision: str) -> list[int]:
    query = urllib.parse.urlencode({"head_sha": revision, "branch": "main", "event": "push", "per_page": 100})
    runs = github_json(f"/repos/{repository}/actions/workflows/ci.yaml/runs?{query}")["workflow_runs"]
    accepted = [run["id"] for run in runs
                if run.get("head_sha") == revision and run.get("head_branch") == "main"
                and run.get("event") == "push" and run.get("status") == "completed"
                and run.get("conclusion") == "success"
                and run.get("repository", {}).get("full_name") == repository
                and run.get("head_repository", {}).get("full_name") == repository
                and run.get("path", "").split("@", 1)[0] == ".github/workflows/ci.yaml"
                and type(run.get("id")) is int and run["id"] > 0]
    if not accepted:
        raise ValueError("The selected staging image has no successful push-main CI run")
    return accepted


def verify_build_artifact(registry: Registry, digest: str, repository: str,
                          revision: str, service: str) -> None:
    expected = {"service": service, "repository": "harbor.internal.api-api-api.com/playground/" + service,
                "source_repository": repository, "source_commit": revision, "source_branch": "main"}
    if service not in SERVICES or repository != "anomaly51/playground-" + service:
        raise ValueError("Unexpected release repository")
    candidate_count, live_artifact = 0, False
    for run_id in successful_build(repository, revision):
        response = github_json(f"/repos/{repository}/actions/runs/{run_id}/artifacts?per_page=100")
        artifacts = response.get("artifacts", [])
        if response.get("total_count", len(artifacts)) > len(artifacts):
            raise ValueError("Incomplete release artifact listing")
        candidates = [item for item in artifacts if item.get("name") == "release-image"]
        candidate_count += len(candidates)
        if candidate_count > MAX_RELEASE_ARTIFACTS:
            raise ValueError("Too many release-image artifacts to verify safely")
        matched, seen_ids = False, set()
        for artifact in candidates:
            if artifact.get("expired") is True:
                continue
            if (artifact.get("expired") is not False or type(artifact.get("id")) is not int
                    or artifact["id"] < 1 or type(artifact.get("size_in_bytes")) is not int
                    or not 0 < artifact["size_in_bytes"] <= MAX_RESPONSE_BYTES):
                raise ValueError("Release artifact metadata is invalid or oversized")
            if artifact["id"] in seen_ids:
                raise ValueError("Duplicate release artifact ID")
            seen_ids.add(artifact["id"])
            live_artifact = True
            body = github_bytes(f"/repos/{repository}/actions/artifacts/{artifact['id']}/zip", archive=True)
            document = release_document(body)
            if any(document[key] != value for key, value in expected.items()):
                raise ValueError("CI artifact does not belong to the selected main release")
            published_digest = document["digest"]
            _, platform_digest = image_manifest(registry, published_digest)
            matched |= digest in {published_digest, platform_digest}
        # Reruns may retain same-name artifacts. Validate every live candidate's
        # identity before accepting a matching immutable image from this run.
        if matched:
            return
    if live_artifact:
        raise ValueError("Staging digest was not published by the successful main CI run")
    raise ValueError("Successful main CI has no release-image artifact; rebuild main before promotion")


def promoted_values(production: dict, staging: dict, service: str, commit: str, revision: str) -> dict:
    if service not in SERVICES or not SHA.fullmatch(commit) or not SHA.fullmatch(revision):
        raise ValueError("Invalid promotion identity")
    if production["_release"].get("policy") != "promote":
        raise ValueError("Production must use manual promotion")
    repository = "harbor.internal.api-api-api.com/playground/" + service
    for values, environment in ((production, "prod"), (staging, "staging")):
        if values["_release"].get("namespace") != "playground-" + environment:
            raise ValueError("Unexpected environment namespace")
        if values["image"]["repository"] != repository:
            raise ValueError("Unexpected image repository")
    if not STAGED_IMAGE.fullmatch(staging["image"].get("tag", "")):
        raise ValueError("Staging must pin its image digest")
    if not SHA.fullmatch(staging["_release"].get("revision", "")):
        raise ValueError("Staging must pin its chart revision")
    result = copy.deepcopy(production)
    result["image"]["tag"] = staging["image"]["tag"]
    result["_release"].update(
        revision=staging["_release"]["revision"], promotedFrom=commit,
        sourceRepository="anomaly51/playground-" + service, sourceCommit=revision,
        sourceBranch="main", imageKeys=["image"],
        runUrl=f"https://github.com/anomaly51/playground-{service}/commit/{revision}")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--staging-commit", default="")
    args = parser.parse_args()
    repository = validate_caller(args.service)
    app = "playground-" + args.service
    commit = args.staging_commit or git("rev-parse", "HEAD")
    if not SHA.fullmatch(commit):
        raise ValueError("Use a full GitOps commit SHA or leave it empty")
    subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], check=True)
    staging = yaml.safe_load(git("show", f"{commit}:apps/{app}/values/staging.yaml"))
    tag = STAGED_IMAGE.fullmatch(staging["image"].get("tag", ""))
    if not tag:
        raise ValueError("Staging does not yet have a registry-verified digest")
    expected_repository = "harbor.internal.api-api-api.com/playground/" + args.service
    if staging["image"]["repository"] != expected_repository:
        raise ValueError("Unexpected staging registry")
    registry = Registry(expected_repository)
    revision = source_revision(registry, tag.group(1), repository)
    verify_build_artifact(registry, tag.group(1), repository, revision, args.service)
    # imageKeys is release metadata, excluded from the rendered application values.
    staging["_release"]["imageKeys"] = ["image"]
    wait(app, "staging", staging, timeout=180)
    path = Path("apps") / app / "values/prod.yaml"
    production = yaml.safe_load(path.read_text())
    proposed = promoted_values(production, staging, args.service, commit, revision)
    validate_chart(app, proposed)
    path.write_text(yaml.safe_dump(proposed, sort_keys=False))
    print(f"Verified healthy {app}/staging, main {revision}, digest {tag.group(1)}")
    print("Production URLs, credentials, resources, storage, and replicas are unchanged.")


if __name__ == "__main__":
    main()
