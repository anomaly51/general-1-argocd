"""Publish immutable images according to the application's deployment policy."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request

import yaml

SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
IMAGE_KEY = re.compile(r"image|images\.[A-Za-z][A-Za-z0-9]*")


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def profile(app: str, environment: str) -> Path:
    if not SLUG.fullmatch(app) or environment not in {"dev", "staging", "prod"}:
        raise ValueError("Invalid application or environment")
    return Path("apps") / app / "values" / f"{environment}.yaml"


def deployment_policy(app: str) -> dict:
    release = yaml.safe_load(profile(app, "prod").read_text())["_release"]
    if release.get("policy", "promote") not in {"promote", "prod-only"}:
        raise ValueError("Unknown deployment policy")
    return release


def automatic_environment(app: str, branch: str, event: str) -> str:
    policy = deployment_policy(app)
    if event == "pull_request":
        return ""  # Pull requests build without publishing or deployment credentials.
    if policy.get("policy") == "prod-only":
        if branch != "main" or event != "push":
            raise ValueError("Prod-only bots deploy exclusively from a push to main")
        return "prod"
    environment = {"main": "staging", "dev": "dev"}.get(branch)
    if not environment or event not in {"push", "workflow_dispatch"}:
        raise ValueError("Only main and dev may publish application environments")
    return environment


def validate_automatic_release(app: str, environment: str, source: dict) -> None:
    if environment != automatic_environment(app, source["branch"], source["event"]):
        raise ValueError("The selected environment does not match the deployment policy")
    policy = deployment_policy(app)
    if policy.get("policy") == "prod-only":
        if source["repository"] != policy.get("sourceRepository"):
            raise ValueError("Only the bot's configured source repository may publish production")
        if any(profile(app, env).exists() for env in ("dev", "staging")):
            raise ValueError("Prod-only bots cannot have dev or staging profiles")


def image_at(values: dict, key: str) -> dict:
    if not IMAGE_KEY.fullmatch(key):
        raise ValueError("Only image or images.<component> may be released")
    result = values
    for part in key.split("."):
        result = result[part]
    if not isinstance(result, dict) or not isinstance(result.get("repository"), str):
        raise ValueError(f"Missing image configuration: {key}")
    return result


def build_plan(app: str, components: list[dict]) -> dict:
    values = yaml.safe_load(profile(app, "prod").read_text())
    if not components or len({c["key"] for c in components}) != len(components):
        raise ValueError("Each component must have a unique image key")
    plan = []
    for component in components:
        if set(component) - {"key", "dockerfile", "context", "build_args"}:
            raise ValueError("Unsupported build component field")
        repository = image_at(values, component["key"])["repository"]
        if not repository.startswith(("harbor.internal.api-api-api.com/", "ghcr.io/anomaly51/")):
            raise ValueError("Builds may only publish to the configured application registries")
        plan.append({"id": component["key"].replace(".", "-"), "repository": repository,
                     "dockerfile": "Dockerfile", "context": ".", "build_args": "", **component})
    return {"include": plan}


def updated_profile(values: dict, images: list[dict], source: dict, chart_revision: str) -> dict:
    if not SHA.fullmatch(source["commit"]):
        raise ValueError("Source commit must be a full SHA")
    if source["branch"] not in {"main", "dev"}:
        raise ValueError("Only main and dev can publish an environment")
    if not images or len({i["key"] for i in images}) != len(images):
        raise ValueError("Expected a unique image for every built component")
    result = copy.deepcopy(values)
    for image in images:
        target = image_at(result, image["key"])
        if target["repository"] != image["repository"]:
            raise ValueError("An image release cannot change its registry/repository")
        if not DIGEST.fullmatch(image["digest"]):
            raise ValueError("Every image must have a verified registry digest")
        expected_tag = "sha-" + source["commit"][:12]
        if image["tag"] != expected_tag:
            raise ValueError("Image tag must identify the exact source commit")
        # Works with charts that render repository:tag, including older pinned charts.
        target["tag"] = f'{expected_tag}@{image["digest"]}'
        if "digest" in target:
            target["digest"] = image["digest"]
    release = result["_release"]
    revision_pattern = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?" if "repository" in release else r"[0-9a-f]{40}"
    if not re.fullmatch(revision_pattern, chart_revision):
        raise ValueError("Pin an exact chart revision")
    release.update(revision=chart_revision, sourceRepository=source["repository"],
                   sourceCommit=source["commit"], sourceBranch=source["branch"],
                   runUrl=source["run_url"], imageKeys=[image["key"] for image in images])
    return result


def release_output(environment: str) -> None:
    if output := os.environ.get("GITHUB_OUTPUT"):
        appset = yaml.safe_load(Path("cluster/applicationsets/apps.yaml").read_text())
        entries = appset["spec"]["generators"][0]["git"]["files"]
        active = {"path": f"apps/*/values/{environment}.yaml"} in entries
        with open(output, "a") as file:
            file.write(f"commit={git('rev-parse', 'HEAD')}\nenvironment={environment}\nactive={str(active).lower()}\n")


def publish(app: str, environment: str, images: list[dict], source: dict, chart_revision: str | None) -> None:
    validate_automatic_release(app, environment, source)
    path = profile(app, environment)
    for attempt in range(5):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{source['repository']}/git/ref/heads/{source['branch']}",
            headers={"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            current_sha = json.load(response)["object"]["sha"]
        if current_sha != source["commit"]:
            print("A newer source commit exists; this superseded build will not update GitOps.")
            return
        # This command runs in a disposable CI checkout; retry from current remote main.
        subprocess.run(["git", "fetch", "origin", "main"], check=True)
        subprocess.run(["git", "reset", "--hard", "origin/main"], check=True)
        validate_automatic_release(app, environment, source)
        if not path.exists():
            print(f"{app}/{environment} has no profile: images were published, no environment was created.")
            return
        values = yaml.safe_load(path.read_text())
        # Automatic production updates image digests; chart changes remain explicit GitOps changes.
        if environment == "prod" and chart_revision and chart_revision != values["_release"]["revision"]:
            raise ValueError("Automatic production cannot override the configured chart pin")
        revision = chart_revision or (values["_release"]["revision"]
            if environment == "prod" or "repository" in values["_release"] else git("rev-parse", "HEAD"))
        updated = updated_profile(values, images, source, revision)
        if updated == values:
            print("This release is already recorded.")
            release_output(environment)
            return
        path.write_text(yaml.safe_dump(updated, sort_keys=False))
        subprocess.run(["git", "diff", "--check"], check=True)
        subprocess.run(["git", "add", "--", str(path)], check=True)
        subprocess.run(["git", "commit", "-m", f"deploy({app}): {environment} {source['commit'][:12]}"], check=True)
        result = subprocess.run(["git", "push", "origin", "HEAD:main"])
        if result.returncode == 0:
            commit = git("rev-parse", "HEAD")
            print(f"Recorded {app}/{environment} at GitOps commit {commit}")
            release_output(environment)
            return
        time.sleep(attempt + 1)
    raise RuntimeError("GitOps main kept changing; no force-push was attempted")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--app", required=True)
    plan.add_argument("--components", required=True)
    environment_parser = commands.add_parser("environment")
    environment_parser.add_argument("--app", required=True)
    environment_parser.add_argument("--branch", required=True)
    environment_parser.add_argument("--event", required=True)
    publish_parser = commands.add_parser("publish")
    publish_parser.add_argument("--app", required=True)
    publish_parser.add_argument("--environment", required=True, choices=["dev", "staging", "prod"])
    publish_parser.add_argument("--images", type=Path, required=True)
    publish_parser.add_argument("--chart-revision")
    args = parser.parse_args()
    if args.command == "plan":
        print(json.dumps(build_plan(args.app, json.loads(args.components)), separators=(",", ":")))
    elif args.command == "environment":
        print(automatic_environment(args.app, args.branch, args.event))
    else:
        images = [json.loads(path.read_text()) for path in sorted(args.images.glob("*.json"))]
        source = {"repository": os.environ["GITHUB_REPOSITORY"], "commit": os.environ["GITHUB_SHA"],
                  "branch": os.environ["GITHUB_REF_NAME"],
                  "event": os.environ["GITHUB_EVENT_NAME"],
                  "run_url": f'https://github.com/{os.environ["GITHUB_REPOSITORY"]}/actions/runs/{os.environ["GITHUB_RUN_ID"]}'}
        publish(args.app, args.environment, images, source, args.chart_revision)


if __name__ == "__main__":
    main()
