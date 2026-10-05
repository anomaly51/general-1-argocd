"""Check the exact GitOps release, optionally synchronizing a manual production release."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request
import urllib.parse

import yaml

from gitops_release import profile, image_at


def request(path: str, body: dict | None = None) -> dict:
    server = os.environ["ARGOCD_SERVER"].rstrip("/")
    if server != "https://argocd.internal.api-api-api.com":
        raise ValueError("Unexpected Argo CD server")
    req = urllib.request.Request(server + "/api/v1/" + path,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Authorization": "Bearer " + os.environ["ARGOCD_AUTH_TOKEN"],
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def matches(application: dict, values: dict, app: str | None = None) -> bool:
    source = application.get("spec", {}).get("source", {})
    try:
        embedded = yaml.safe_load(source.get("helm", {}).get("values", ""))
    except yaml.YAMLError:
        return False
    expected = {key: value for key, value in values.items() if key != "_release"}
    release = values["_release"]
    if "repository" in release:
        if source.get("repoURL") != release["repository"]:
            return False
        expected_chart = release.get("chart", app)
        if expected_chart is not None and source.get("chart") != expected_chart:
            return False
        if source.get("path"):
            return False
    elif app is not None and (source.get("repoURL") != "https://github.com/anomaly51/general-1-argocd.git"
                              or source.get("path") != f"apps/{app}" or source.get("chart")):
        return False
    return source.get("targetRevision") == release["revision"] and embedded == expected


def healthy(application: dict) -> bool:
    status = application.get("status", {})
    compared = status.get("sync", {}).get("comparedTo", {}).get("source", {})
    desired = application.get("spec", {}).get("source", {})
    return (status.get("sync", {}).get("status") == "Synced"
            and status.get("health", {}).get("status") == "Healthy"
            and compared.get("targetRevision") == desired.get("targetRevision")
            and compared.get("helm", {}).get("values") == desired.get("helm", {}).get("values")
            and all((compared.get(key) or "") == (desired.get(key) or "") for key in ("repoURL", "chart", "path"))
            and application.get("operation") is None
            and status.get("operationState", {}).get("phase") not in {"Running", "Terminating"})


def deployment_ready(deployment: dict, expected: set[str]) -> set[str]:
    metadata, spec, status = (deployment.get(key, {}) for key in ("metadata", "spec", "status"))
    replicas = spec.get("replicas", 1)
    if metadata.get("deletionTimestamp") or spec.get("paused") or replicas < 1:
        return set()
    if status.get("observedGeneration", 0) < metadata.get("generation", 1):
        return set()
    if any(status.get(key, 0) != replicas for key in ("updatedReplicas", "readyReplicas", "availableReplicas")):
        return set()
    if status.get("replicas", 0) != replicas:
        return set()
    containers = spec.get("template", {}).get("spec", {}).get("containers", [])
    return expected.intersection(container.get("image") for container in containers)


def live_workloads_ready(name: str, application: dict, values: dict) -> bool:
    expected = {f"{image_at(values, key)['repository']}:{image_at(values, key)['tag']}"
                for key in values["_release"].get("imageKeys", [])}
    if not expected:
        return False
    found = set()
    for resource in application.get("status", {}).get("resources", []):
        if resource.get("kind") != "Deployment" or resource.get("group") != "apps":
            continue
        query = urllib.parse.urlencode({"namespace": resource["namespace"], "resourceName": resource["name"],
                                        "group": "apps", "version": "v1", "kind": "Deployment"})
        response = request(f"applications/{name}/resource?{query}")
        manifest = response.get("manifest", {})
        if isinstance(manifest, str):
            manifest = json.loads(manifest)
        found.update(deployment_ready(manifest, expected))
    return found == expected


def wait(app: str, environment: str, values: dict, sync: bool = False, timeout: int = 1200) -> None:
    if sync and environment != "prod":
        raise ValueError("Only production is explicitly synchronized by this workflow")
    name = f"apps-{app}" if environment == "prod" else f"{app}-{environment}"
    deadline = time.monotonic() + timeout
    synced = not sync
    while time.monotonic() < deadline:
        try:
            application = request(f"applications/{name}?refresh=normal")
            if matches(application, values, app):
                if not synced:
                    if application.get("operation"):
                        raise RuntimeError("Another synchronization is already running")
                    request(f"applications/{name}/sync", {"prune": True, "dryRun": False,
                                                          "revision": values["_release"]["revision"]})
                    synced = True
                    print(f"Requested production synchronization: {name}", flush=True)
                elif healthy(application) and live_workloads_ready(name, application, values):
                    print(f"Verified exact release: {name} is Synced/Healthy", flush=True)
                    return
            print(f"Waiting for {name} to reconcile the selected release", flush=True)
        except urllib.error.HTTPError as error:
            if error.code not in {404, 429, 502, 503, 504}:
                raise RuntimeError(f"Argo CD request rejected: HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError):
            print("Argo CD temporarily unreachable; retrying", flush=True)
        time.sleep(10)
    raise TimeoutError(f"The selected release of {name} did not become Synced/Healthy")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["wait", "sync"])
    parser.add_argument("--app", required=True)
    parser.add_argument("--environment", choices=["dev", "staging", "prod"], required=True)
    parser.add_argument("--values", type=Path)
    args = parser.parse_args()
    values = yaml.safe_load((args.values or profile(args.app, args.environment)).read_text())
    wait(args.app, args.environment, values, sync=args.command == "sync")


if __name__ == "__main__":
    main()
