"""Prepare a production release from a healthy staging snapshot, retaining prod settings."""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

import yaml

from argocd_release import wait
from gitops_release import SHA, chart_name, image_at, profile, updated_profile, validate_chart_pin
from registry_release import Registry


def promoted_values(production: dict, staging: dict, commit: str) -> dict:
    if production["_release"].get("policy") == "prod-only":
        raise ValueError("Prod-only bots deploy automatically from main; Promote is not used")
    source, target = staging["_release"], production["_release"]
    for release in (source, target):
        validate_chart_pin(release)
    if any(source.get(key) != target.get(key) for key in ("repository", "chart")):
        raise ValueError("Changing chart source or name needs an explicit migration")
    if source.get("sourceBranch") != "main" or not SHA.fullmatch(source.get("sourceCommit", "")):
        raise ValueError("Only a CI release built from source main can be promoted")
    keys = source.get("imageKeys", [])
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("No complete CI release metadata; build source main first")
    result = copy.deepcopy(production)
    for key in keys:
        image = image_at(staging, key)
        target_image = image_at(result, key)
        if image["repository"] != target_image["repository"]:
            raise ValueError("Changing the image repository needs an explicit migration")
        if not re.fullmatch(r"sha-[0-9a-f]{12}@sha256:[0-9a-f]{64}", image.get("tag", "")):
            raise ValueError("Staging must reference an immutable image digest")
        if image["tag"].split("@", 1)[0] != "sha-" + source["sourceCommit"][:12]:
            raise ValueError("Staging images do not belong to the recorded source commit")
        target_image["tag"] = image["tag"]
        if "digest" in image:
            target_image["digest"] = image["digest"]
    for key in ["revision", "sourceRepository", "sourceCommit", "sourceBranch", "runUrl", "imageKeys"]:
        result["_release"][key] = copy.deepcopy(source[key])
    result["_release"]["promotedFrom"] = commit
    return result


def validate_chart(app: str, values: dict) -> None:
    release = values["_release"]
    name = chart_name(app, release)
    with tempfile.TemporaryDirectory(prefix="production-chart-") as temporary:
        directory = Path(temporary)
        value_file = directory / "production.yaml"
        value_file.write_text(yaml.safe_dump(values))
        if "repository" in release:
            chart = f"oci://{release['repository']}/{name}"
            extra = ["--version", release["revision"]]
        else:
            archive = subprocess.check_output(["git", "archive", release["revision"], "--", f"apps/{app}"])
            import io, tarfile
            with tarfile.open(fileobj=io.BytesIO(archive)) as files:
                files.extractall(directory, filter="data")
            chart = str(directory / "apps" / app)
            extra = []
        subprocess.run(["helm", "template", app, chart, "--namespace", release.get("namespace", "apps"),
                        "-f", str(value_file), *extra], check=True, stdout=subprocess.DEVNULL)


def production_only(app: str, production: dict, source_commit: str, chart_version: str | None) -> dict:
    if production["_release"].get("policy") == "prod-only":
        raise ValueError("Prod-only bots deploy automatically from main; Promote is not used")
    if profile(app, "staging").exists():
        raise ValueError("This application has staging; promote its healthy staging snapshot")
    if not SHA.fullmatch(source_commit):
        raise ValueError("Select the full source commit built from main")
    candidates = {"image": production["image"]} if "image" in production else {
        "images." + key: value for key, value in production.get("images", {}).items()
        if value["repository"].startswith(("harbor.internal.api-api-api.com/", "ghcr.io/anomaly51/"))}
    images, repositories = [], set()
    for key, value in candidates.items():
        digest, repository = Registry(value["repository"]).image("main-sha-" + source_commit[:12], source_commit, require_main_label=app != "cutline-studio")
        repositories.add(repository)
        images.append({"key": key, "repository": value["repository"], "tag": "sha-" + source_commit[:12], "digest": digest})
    if len(repositories) != 1:
        raise ValueError("All release images must belong to the same source repository")
    repository = repositories.pop()
    release = production["_release"]
    if "repository" in release:
        if not chart_version or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", chart_version):
            raise ValueError("Select the exact OCI chart version from the source CI run")
        chart = yaml.safe_load(subprocess.check_output([
            "helm", "show", "chart", f"oci://{release['repository']}/{chart_name(app, release)}", "--version", chart_version], text=True))
        annotations = chart.get("annotations", {})
        if annotations.get("io.cutline.studio.source-sha") != source_commit or annotations.get("io.cutline.studio.source-branch") != "main":
            raise ValueError("OCI chart was not published from the selected main commit")
        for image in images:
            component = image["key"].split(".")[-1]
            if annotations.get(f"io.cutline.studio.{component}-digest") != image["digest"]:
                raise ValueError("Chart and image digests do not belong to the same release")
        revision = chart_version
    else:
        if chart_version:
            raise ValueError("Git charts do not take an OCI chart version")
        revision = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    return updated_profile(production, images, {"repository": repository, "commit": source_commit,
        "branch": "main", "run_url": f"https://github.com/{repository}/commit/{source_commit}"}, revision)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", required=True)
    parser.add_argument("--commit", default="")
    parser.add_argument("--source-commit", default="")
    parser.add_argument("--chart-version", default="")
    args = parser.parse_args()
    if bool(args.commit) == bool(args.source_commit):
        raise ValueError("Provide exactly one staging commit or prod-only source commit")
    staging_path, production_path = profile(args.app, "staging"), profile(args.app, "prod")
    production = yaml.safe_load(production_path.read_text())
    if args.source_commit:
        proposed = production_only(args.app, production, args.source_commit, args.chart_version)
    else:
        if not SHA.fullmatch(args.commit):
            raise ValueError("Select a full GitOps staging commit SHA")
        subprocess.run(["git", "merge-base", "--is-ancestor", args.commit, "HEAD"], check=True)
        appset = yaml.safe_load(subprocess.check_output(
            ["git", "show", f"{args.commit}:cluster/applicationsets/apps.yaml"], text=True))
        entries = appset["spec"]["generators"][0]["git"]["files"]
        if {"path": "apps/*/values/staging.yaml"} not in entries:
            raise ValueError("Staging is not active in the selected GitOps commit")
        staging = yaml.safe_load(subprocess.check_output(["git", "show", f"{args.commit}:{staging_path}"], text=True))
        proposed = promoted_values(production, staging, args.commit)
        wait(args.app, "staging", staging, timeout=180)
    validate_chart(args.app, proposed)
    production_path.write_text(yaml.safe_dump(proposed, sort_keys=False))
    print(f"Prepared {args.app}; production settings retained, image digests verified")


if __name__ == "__main__":
    main()
