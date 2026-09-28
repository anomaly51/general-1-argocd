"""Read verified image digests and source labels from the configured registries."""
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.oci.image.manifest.v1+json",
                    "application/vnd.docker.distribution.manifest.list.v2+json", "application/vnd.docker.distribution.manifest.v2+json"])


class Registry:
    def __init__(self, repository):
        self.host, self.repository = repository.split("/", 1)
        if self.host not in {"harbor.internal.api-api-api.com", "ghcr.io"}:
            raise ValueError("Unsupported release registry")
        self.bearer = None

    def get(self, suffix):
        url = f"https://{self.host}/v2/{self.repository}/{suffix}"
        for attempt in range(2):
            headers = {"Accept": ACCEPT}
            if self.bearer:
                headers["Authorization"] = "Bearer " + self.bearer
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
                    return response.read(), response.headers
            except urllib.error.HTTPError as error:
                if error.code != 401 or attempt:
                    raise RuntimeError(f"Registry release is unavailable: HTTP {error.code}") from None
                fields = dict(re.findall(r'(\w+)="([^"]+)"', error.headers.get("WWW-Authenticate", "")))
                realm = urllib.parse.urlsplit(fields.get("realm", ""))
                if realm.scheme != "https" or realm.netloc != self.host:
                    raise ValueError("Unexpected registry authentication endpoint")
                query = urllib.parse.urlencode({"service": fields.get("service", self.host),
                                               "scope": f"repository:{self.repository}:pull"})
                auth = {}
                if self.host.startswith("harbor."):
                    credentials = os.environ["REGISTRY_USERNAME"] + ":" + os.environ["REGISTRY_PASSWORD"]
                    auth["Authorization"] = "Basic " + base64.b64encode(credentials.encode()).decode()
                with urllib.request.urlopen(urllib.request.Request(fields["realm"] + "?" + query, headers=auth), timeout=30) as response:
                    result = json.load(response)
                self.bearer = result.get("token") or result["access_token"]
        raise RuntimeError("Registry authentication failed")

    def manifest(self, reference):
        body, headers = self.get("manifests/" + reference)
        digest = "sha256:" + hashlib.sha256(body).hexdigest()
        if headers.get("Docker-Content-Digest") != digest:
            raise ValueError("Registry manifest digest mismatch")
        return json.loads(body), digest

    def image(self, reference, source_commit, require_main_label=True):
        manifest, digest = self.manifest(reference)
        if "manifests" in manifest:
            platform = next(item for item in manifest["manifests"] if item.get("platform", {}).get("os") == "linux"
                            and item.get("platform", {}).get("architecture") == "amd64")
            manifest, _ = self.manifest(platform["digest"])
        body, _ = self.get("blobs/" + manifest["config"]["digest"])
        if "sha256:" + hashlib.sha256(body).hexdigest() != manifest["config"]["digest"]:
            raise ValueError("Image configuration digest mismatch")
        labels = json.loads(body).get("config", {}).get("Labels", {})
        if labels.get("org.opencontainers.image.revision") != source_commit or (require_main_label and labels.get("io.gitops.source-branch") != "main"):
            raise ValueError("The image was not published by source main CI at the selected commit")
        source = labels.get("org.opencontainers.image.source", "")
        if not re.fullmatch(r"https://github.com/anomaly51/[A-Za-z0-9_.-]+", source):
            raise ValueError("Unexpected source repository in image provenance")
        return digest, source.removeprefix("https://github.com/")
