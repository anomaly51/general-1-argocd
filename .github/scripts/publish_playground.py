"""Publish a successful same-run playground build to its GitOps environment."""
import argparse
import json
import os
from pathlib import Path

from gitops_release import DIGEST, SHA, publish
from registry_release import Registry

SERVICES = {"order-service", "pricing-service", "inventory-service", "event-hub",
            "analytics-service", "shell", "topology-mfe", "traffic-mfe"}


def validate_release(service: str, artifact: dict, source: dict) -> dict:
    if service not in SERVICES or source["repository"] != f"anomaly51/playground-{service}":
        raise ValueError("Only this service's source repository may publish its profile")
    if source["event"] != "push" or source["branch"] not in {"main", "dev"}:
        raise ValueError("Only trusted pushes to dev/main may publish")
    if not SHA.fullmatch(source["commit"]):
        raise ValueError("Expected a full source commit SHA")
    expected = {"service": service,
                "repository": f"harbor.internal.api-api-api.com/playground/{service}",
                "source_repository": source["repository"], "source_commit": source["commit"],
                "source_branch": source["branch"]}
    if set(artifact) != set(expected) | {"digest"} or any(artifact.get(k) != v for k, v in expected.items()):
        raise ValueError("The same-run release artifact does not match the caller/source")
    if not DIGEST.fullmatch(str(artifact["digest"])):
        raise ValueError("Expected a verified image digest")
    return {"key": "image", "repository": expected["repository"],
            "tag": f'{source["branch"]}-{source["commit"]}', "digest": artifact["digest"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    args = parser.parse_args()
    source = {"repository": os.environ["GITHUB_REPOSITORY"], "commit": os.environ["GITHUB_SHA"],
              "branch": os.environ["GITHUB_REF_NAME"], "event": os.environ["GITHUB_EVENT_NAME"],
              "run_url": f'https://github.com/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'}
    image = validate_release(args.service, json.loads(args.artifact.read_text()), source)
    registry = Registry(image["repository"])
    digest, repository = registry.image(image["digest"], source["commit"],
                                         require_main_label=False, source_branch=source["branch"])
    if digest != image["digest"] or repository != source["repository"]:
        raise ValueError("Registry provenance does not match the successful build")
    # Also verify the human-readable immutable tag resolves to that exact manifest.
    if registry.manifest(image["tag"])[1] != digest:
        raise ValueError("Source tag no longer resolves to the recorded digest")
    app = f"playground-{args.service}"
    environment = "staging" if source["branch"] == "main" else "dev"
    # Image publishing must never upgrade or replace a chart implicitly.
    publish(app, environment, [image], source, None, preserve_chart=True)


if __name__ == "__main__":
    main()
