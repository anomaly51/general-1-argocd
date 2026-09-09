"""Cutline deploy-ready marker publisher and credential-free GitOps consumer.

Vendored byte-for-byte as general-1-argocd/scripts/cutline-studio-sync.py.
Registry credentials are read only from the environment and never logged.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REGISTRY = "harbor.internal.api-api-api.com"
SOURCE_REPOSITORY = "anomaly51/cutline-studio"
MARKER_REPOSITORY = "cutline-studio/deploy-ready"
LABEL = "io.cutline.studio.release"
VALUES_PATH = "apps/cutline-studio/values.yaml"
REPOSITORIES = {name: f"{REGISTRY}/cutline-studio/{name}" for name in ("api", "frontend")}
TAG_RE = re.compile(r"sha-[0-9a-f]{12}\Z")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])


class RegistryError(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RegistryError("Registry redirect rejected; credentials were not forwarded")


class Registry:
    def __init__(self, username: str, password: str):
        if not username or not password:
            raise RegistryError("Set HARBOR_USERNAME and HARBOR_PASSWORD")
        self.basic = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.tokens: dict[str, str] = {}
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, url: str, headers: dict | None = None) -> tuple[bytes, object]:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != REGISTRY or parsed.username:
            raise RegistryError("Registry URL outside the fixed trusted TLS endpoint")
        request = urllib.request.Request(url, headers=headers or {})
        with self.opener.open(request, timeout=30) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise RegistryError("Registry metadata response exceeds limit")
            return body, response.headers

    def token(self, repository: str, challenge: str) -> str:
        if not challenge.lower().startswith("bearer "):
            raise RegistryError("Expected Harbor bearer authentication")
        fields = dict(re.findall(r'(\w+)="([^"\r\n]*)"', challenge))
        realm = fields.get("realm", "")
        parsed = urllib.parse.urlsplit(realm)
        if parsed.scheme != "https" or parsed.netloc != REGISTRY or parsed.username:
            raise RegistryError("Untrusted Harbor authentication realm")
        query = urllib.parse.urlencode({"service": fields.get("service", "harbor-registry"), "scope": f"repository:{repository}:pull"})
        body, _ = self.request(realm + ("&" if parsed.query else "?") + query, {"Authorization": "Basic " + self.basic})
        payload = json.loads(body)
        token = payload.get("token") or payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RegistryError("Harbor did not issue a pull token")
        self.tokens[repository] = token
        return token

    def get(self, repository: str, suffix: str, missing_ok: bool = False) -> tuple[bytes, object] | None:
        if repository not in {MARKER_REPOSITORY, "cutline-studio/api", "cutline-studio/frontend"}:
            raise RegistryError("Unexpected registry repository")
        url = f"https://{REGISTRY}/v2/{repository}/{suffix}"
        headers = {"Accept": ACCEPT}
        if repository in self.tokens:
            headers["Authorization"] = "Bearer " + self.tokens[repository]
        for attempt in range(2):
            try:
                return self.request(url, headers)
            except urllib.error.HTTPError as exc:
                if exc.code == 404 and missing_ok:
                    return None
                if exc.code == 401 and attempt == 0:
                    headers["Authorization"] = "Bearer " + self.token(repository, exc.headers.get("WWW-Authenticate", ""))
                    continue
                raise RegistryError(f"Harbor metadata request failed with HTTP {exc.code}") from None
        raise RegistryError("Harbor authentication failed")

    def manifest(self, repository: str, reference: str, missing_ok: bool = False):
        if not (TAG_RE.fullmatch(reference) or DIGEST_RE.fullmatch(reference) or reference == "main"):
            raise RegistryError("Unexpected image reference")
        response = self.get(repository, f"manifests/{reference}", missing_ok)
        if response is None:
            return None
        body, headers = response
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        advertised = headers.get("Docker-Content-Digest")
        if (advertised and advertised != digest) or (reference.startswith("sha256:") and reference != digest):
            raise RegistryError("Registry manifest digest mismatch")
        return json.loads(body), digest

    def marker(self, reference: str, missing_ok: bool = False):
        result = self.manifest(MARKER_REPOSITORY, reference, missing_ok)
        if result is None:
            return None
        manifest, _ = result
        # Buildx may represent a single-platform marker as an OCI index.
        if "manifests" in manifest:
            descriptors = manifest["manifests"]
            if len(descriptors) != 1 or not DIGEST_RE.fullmatch(descriptors[0].get("digest", "")):
                raise RegistryError("Deploy-ready marker must have exactly one image manifest")
            manifest, _ = self.manifest(MARKER_REPOSITORY, descriptors[0]["digest"])
        config_digest = manifest.get("config", {}).get("digest", "")
        # BuildKit's FROM scratch output represents no layers as JSON null.
        # Require the field, and never accept an actual filesystem layer.
        if not DIGEST_RE.fullmatch(config_digest) or "layers" not in manifest or manifest["layers"] not in ([], None):
            raise RegistryError("Deploy-ready marker must be a layer-free image config")
        body, _ = self.get(MARKER_REPOSITORY, f"blobs/{config_digest}")
        if "sha256:" + hashlib.sha256(body).hexdigest() != config_digest:
            raise RegistryError("Deploy-ready config digest mismatch")
        config = json.loads(body)
        rootfs = config.get("rootfs", {})
        if rootfs.get("type") != "layers" or "diff_ids" not in rootfs or rootfs["diff_ids"] not in ([], None):
            raise RegistryError("Deploy-ready marker config must have no filesystem layers")
        label = config.get("config", {}).get("Labels", {}).get(LABEL)
        if not isinstance(label, str) or len(label) > 16384:
            raise RegistryError("Missing or oversized deploy-ready release label")
        marker = json.loads(label)
        validate_marker(marker)
        if reference != "main" and marker["tag"] != reference:
            raise RegistryError("Immutable marker tag does not match its source SHA")
        return marker


def validate_marker(marker: dict) -> None:
    expected = {"schema", "source_repository", "channel", "source_sha", "source_run_number", "source_run_id", "tag", "images"}
    if not isinstance(marker, dict) or set(marker) != expected:
        raise ValueError("Unexpected deploy-ready marker schema")
    if type(marker["schema"]) is not int or marker["schema"] != 1:
        raise ValueError("Unsupported deploy-ready schema version")
    if marker["source_repository"] != SOURCE_REPOSITORY or marker["channel"] != "main":
        raise ValueError("Unexpected source repository or release channel")
    sha = marker["source_sha"]
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha) or marker["tag"] != f"sha-{sha[:12]}":
        raise ValueError("Marker tag must match its full immutable source SHA")
    for key in ("source_run_number", "source_run_id"):
        if type(marker[key]) is not int or not 0 < marker[key] < 2**53:
            raise ValueError("Invalid source workflow sequence")
    if not isinstance(marker["images"], dict) or set(marker["images"]) != set(REPOSITORIES):
        raise ValueError("Marker requires exactly API and frontend images")
    for component, repository in REPOSITORIES.items():
        image = marker["images"][component]
        if not isinstance(image, dict) or set(image) != {"repository", "tag", "digest"}:
            raise ValueError("Unexpected image descriptor schema")
        if image["repository"] != repository or image["tag"] != marker["tag"] or not isinstance(image["digest"], str) or not DIGEST_RE.fullmatch(image["digest"]):
            raise ValueError("Image reference or digest does not match the release")


def verify_images(registry: Registry, marker: dict) -> None:
    for component, image in marker["images"].items():
        _, digest = registry.manifest(f"cutline-studio/{component}", marker["tag"])
        if digest != image["digest"]:
            raise ValueError("Image tag content no longer matches the tested release digest")


def image_lines(text: str) -> dict[str, tuple[int, str]]:
    lines = text.splitlines(keepends=True)
    roots = [i for i, line in enumerate(lines) if re.fullmatch(r"images:\s*(?:#.*)?(?:\r?\n)?", line)]
    if len(roots) != 1:
        raise ValueError("Expected one top-level images mapping")
    start = roots[0] + 1
    end = next((i for i in range(start, len(lines)) if re.match(r"^[^\s#]", lines[i])), len(lines))
    result = {}
    for component, repository in REPOSITORIES.items():
        matches = [i for i in range(start, end) if re.fullmatch(rf"  {component}:\s*(?:#.*)?(?:\r?\n)?", lines[i])]
        if len(matches) != 1:
            raise ValueError(f"Expected one images.{component} mapping")
        first = matches[0] + 1
        last = next((i for i in range(first, end) if re.match(r"^  [^\s#]", lines[i])), end)
        fields = {}
        for field in ("repository", "tag"):
            candidates = [i for i in range(first, last) if re.match(rf"^    {field}:", lines[i])]
            if len(candidates) != 1:
                raise ValueError(f"Expected one images.{component}.{field}")
            index = candidates[0]
            match = re.fullmatch(rf"    {field}:\s*([\w./:-]+|'[^']*'|\"[^\"]*\")\s*(?:#.*)?(?:\r?\n)?", lines[index])
            if not match:
                raise ValueError("Unsupported image scalar syntax")
            fields[field] = (index, match.group(1).strip("'\""))
        if fields["repository"][1] != repository:
            raise ValueError("Unexpected image repository in GitOps values")
        result[component] = fields["tag"]
    return result


def update_tags(text: str, tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise ValueError("Invalid deployment tag")
    lines = text.splitlines(keepends=True)
    for index, _ in image_lines(text).values():
        match = re.fullmatch(r"(    tag:\s*)([\w./:-]+|'[^']*'|\"[^\"]*\")(\s*(?:#.*)?(?:\r?\n)?)", lines[index])
        quote = match.group(2)[0] if match.group(2)[0] in "'\"" else ""
        lines[index] = f"{match.group(1)}{quote}{tag}{quote}{match.group(3)}"
    return "".join(lines)


def permitted_release(candidate: dict, previous: dict | None, current_tag: str) -> bool:
    validate_marker(candidate)
    if current_tag == candidate["tag"]:
        return False
    if current_tag == "bootstrap-pending":
        return True
    if not TAG_RE.fullmatch(current_tag) or previous is None:
        raise ValueError("Current deployment has no valid immutable release marker")
    validate_marker(previous)
    if previous["tag"] != current_tag:
        raise ValueError("Previous release marker does not match deployed tags")
    if candidate["source_run_number"] <= previous["source_run_number"] or candidate["source_run_id"] <= previous["source_run_id"]:
        raise ValueError("Refusing stale or out-of-order deployment; revert using a new source commit")
    return True


def sync(registry: Registry, values: Path, dry_run: bool = False) -> None:
    with values.open("r", newline="") as stream:
        original = stream.read()
    tags = {entry[1] for entry in image_lines(original).values()}
    if len(tags) != 1:
        raise ValueError("Refusing to overwrite mixed API/frontend deployment tags")
    current_tag = next(iter(tags))
    candidate = registry.marker("main", missing_ok=True)
    if candidate is None:
        print("No deploy-ready main marker yet; no GitOps change")
        return
    immutable = registry.marker(candidate["tag"])
    if immutable != candidate:
        raise ValueError("Main channel differs from its immutable release marker")
    verify_images(registry, candidate)
    previous = registry.marker(current_tag) if current_tag not in {"bootstrap-pending", candidate["tag"]} else None
    if not permitted_release(candidate, previous, current_tag):
        print("Both deployed tags already match the tested release")
        return
    updated = update_tags(original, candidate["tag"])
    if not dry_run:
        with values.open("w", newline="") as stream:
            stream.write(updated)
    print(f"{'Would deploy' if dry_run else 'Prepared'} {candidate['tag']}; only two image tag scalars changed")


def publish(registry: Registry) -> None:
    sha = os.environ.get("GITHUB_SHA", "")
    if os.environ.get("GITHUB_REPOSITORY") != SOURCE_REPOSITORY or os.environ.get("GITHUB_REF") != "refs/heads/main" or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("Marker publication is restricted to the source main workflow")
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    if head != sha:
        print("Source main advanced; superseded build will not publish a deployment marker")
        output("published", "false")
        return
    tag = f"sha-{sha[:12]}"
    marker = {
        "schema": 1, "source_repository": SOURCE_REPOSITORY, "channel": "main",
        "source_sha": sha, "source_run_number": int(os.environ["GITHUB_RUN_NUMBER"]),
        "source_run_id": int(os.environ["GITHUB_RUN_ID"]), "tag": tag, "images": {},
    }
    for component, repository in REPOSITORIES.items():
        _, digest = registry.manifest(f"cutline-studio/{component}", tag)
        marker["images"][component] = {"repository": repository, "tag": tag, "digest": digest}
    validate_marker(marker)
    existing = registry.marker(tag, missing_ok=True)
    if existing:
        if existing["source_sha"] != sha or existing["images"] != marker["images"]:
            raise ValueError("Immutable marker already exists with different tested content")
        marker = existing
    channel = registry.marker("main", missing_ok=True)
    if channel and channel["tag"] != tag:
        permitted_release(marker, channel, channel["tag"])
    image = f"{REGISTRY}/{MARKER_REPOSITORY}"
    if existing:
        subprocess.run(["docker", "buildx", "imagetools", "create", "--prefer-index=false", "--tag", image + ":main", image + ":" + tag], check=True)
    else:
        with tempfile.TemporaryDirectory(prefix="cutline-release-") as directory:
            encoded = json.dumps(json.dumps(marker, sort_keys=True, separators=(",", ":")))
            Path(directory, "Dockerfile").write_text(f"FROM scratch\nLABEL {LABEL}={encoded}\n")
            subprocess.run(["docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false", "--push", "--tag", image + ":" + tag, "--tag", image + ":main", directory], check=True)
    if registry.marker("main") != marker or registry.marker(tag) != marker:
        raise ValueError("Published channel and immutable marker do not match")
    print(f"Published deploy-ready {tag} after both build/runtime gates")
    output("published", "true")


def output(name: str, value: str) -> None:
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
            stream.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("publish", "sync"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        registry = Registry(os.environ.get("HARBOR_USERNAME", ""), os.environ.get("HARBOR_PASSWORD", ""))
        if args.mode == "publish":
            if args.dry_run:
                raise ValueError("Publish dry-run is not supported; use isolated tests")
            publish(registry)
        else:
            sync(registry, Path(VALUES_PATH), args.dry_run)
    except (RegistryError, ValueError, KeyError, TypeError, OSError, urllib.error.URLError, subprocess.CalledProcessError) as exc:
        # Do not dump requests, responses, credentials, or exception payloads.
        if isinstance(exc, (RegistryError, ValueError)) and not isinstance(exc, json.JSONDecodeError):
            print(f"Release guard failed: {exc}")
        else:
            print(f"Release guard failed: {type(exc).__name__}; details omitted")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
