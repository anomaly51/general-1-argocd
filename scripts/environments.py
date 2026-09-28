#!/usr/bin/env python3
"""Validate environments; promote release versions without copying environment config."""
import argparse
import copy
from pathlib import Path
import re
import subprocess
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = "https://github.com/anomaly51/general-1-argocd.git"
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")
VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
RELEASE_FIELDS = ("chartRepo", "chartRevision", "chartPath", "chartName", "releaseValues")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return yaml.safe_load(path.read_text())


def descriptor_path(root, product, environment):
    require(bool(SLUG.fullmatch(product)), "Invalid product name")
    require(environment in {"dev", "staging", "prod"}, "Unknown environment")
    return root / "products" / product / "environments" / environment / "deployment.yaml"


def validate_descriptor(data, path, root):
    product, _, environment, _ = path.relative_to(root / "products").parts
    require(data["product"] == product and data["environment"] == environment,
            f"{path}: product/environment must match the directory")
    require(bool(SLUG.fullmatch(product)), f"{path}: invalid product")
    require(environment in {"dev", "staging", "prod"}, f"{path}: unknown environment")
    require(isinstance(data["enabled"], str) and data["enabled"] in {"true", "false"},
            f'{path}: enabled must be the string "true" or "false"')
    require(bool(SLUG.fullmatch(data["namespace"])), f"{path}: invalid namespace")
    if environment != "prod":
        require(data["namespace"] == f"{product}-{environment}",
                f"{path}: nonprod must use its own product/environment namespace")
    require(bool(data["components"]), f"{path}: no components")
    components = set()
    for c in data["components"]:
        name = c["component"]
        require(bool(SLUG.fullmatch(name)) and name not in components,
                f"{path}: invalid/duplicate component")
        components.add(name)
        for key in ("applicationName", "releaseName"):
            require(bool(SLUG.fullmatch(c[key])), f"{path}: invalid {key}")
        if c["chartName"]:
            require(c["chartRepo"] == "harbor.internal.api-api-api.com/cutline-studio"
                    and c["chartName"] == "cutline-studio" and not c["chartPath"],
                    f"{path}: unsupported OCI source")
            require(bool(VERSION.fullmatch(c["chartRevision"])),
                    f"{path}: OCI chart version must be exact")
        else:
            require(c["chartRepo"] == REPO and bool(SHA.fullmatch(c["chartRevision"])),
                    f"{path}: Git charts must use this repository and a full commit SHA")
            require(bool(re.fullmatch(r"apps/[a-z0-9-]+", c["chartPath"])),
                    f"{path}: invalid chart path")
        require(isinstance(c["releaseValues"], dict)
                and set(c["releaseValues"]) <= {"image", "images"},
                f"{path}: releaseValues may contain only image/images")
        values = c["environmentValues"]
        require(isinstance(values, dict), f"{path}: environmentValues must be a mapping")
        require(not ({"image", "images"} & set(values)),
                f"{path}: image versions belong in deployment.yaml")
        if environment != "prod":
            expected = f"apps/{product}/{environment}/"
            require(values["vault"]["envPath"].startswith(expected)
                    and values["registry"]["vaultPath"].startswith(expected)
                    and values["vault"]["role"].startswith(f"{product}-{environment}-"),
                    f"{path}: nonprod must use separate Vault paths/roles")
    return data


def inventory(root):
    result = []
    names = set()
    for path in sorted((root / "products").glob("*/environments/*/deployment.yaml")):
        data = validate_descriptor(read(path), path, root)
        for c in data["components"]:
            require(c["applicationName"] not in names,
                    f"Duplicate Application: {c['applicationName']}")
            names.add(c["applicationName"])
        result.append((path, data))
    require(bool(result), "No environment definitions found")
    return result


def promote_release(staging, production):
    require(staging["enabled"] == "true", "Staging is disabled; nothing verified to promote")
    require(production["enabled"] == "true", "Production is disabled")
    require(staging["environment"] == "staging" and production["environment"] == "prod",
            "Promotion must be staging -> prod")
    require(staging["product"] == production["product"], "Product mismatch")
    sources = {c["component"]: c for c in staging["components"]}
    require(set(sources) == {c["component"] for c in production["components"]},
            "Staging and production must have the same components")
    result = copy.deepcopy(production)
    for c in result["components"]:
        source = sources[c["component"]]
        require(all(c[k] == source[k] for k in ("chartRepo", "chartPath", "chartName")),
                "Changing chart identity requires a reviewed configuration migration")
        for field in RELEASE_FIELDS:
            c[field] = copy.deepcopy(source[field])
    return result


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


def promote(root, product, staging_commit, dry_run):
    require(bool(SHA.fullmatch(staging_commit)), "Use a full, immutable staging GitOps commit SHA")
    subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", staging_commit, "HEAD"], check=True)
    stage_path = descriptor_path(root, product, "staging")
    prod_path = descriptor_path(root, product, "prod")
    staging = yaml.safe_load(git(root, "show", f"{staging_commit}:{stage_path.relative_to(root)}"))
    validate_descriptor(staging, stage_path, root)
    production = validate_descriptor(read(prod_path), prod_path, root)
    result = promote_release(staging, production)
    result["promotedFrom"] = staging_commit
    if dry_run:
        print(yaml.safe_dump(result, sort_keys=False), end="")
    else:
        prod_path.write_text(yaml.safe_dump(result, sort_keys=False))
        print(f"Updated {prod_path.relative_to(root)} from staging at {staging_commit}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    commands.add_parser("list")
    command = commands.add_parser("promote")
    command.add_argument("--product", required=True)
    command.add_argument("--staging-commit", required=True)
    command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    entries = inventory(root)
    if args.command == "validate":
        active = sum(len(d["components"]) for _, d in entries if d["enabled"] == "true")
        print(f"Validated {len(entries)} environments; {active} active Applications")
    elif args.command == "list":
        for _, d in entries:
            print(f"{d['product']:30} {d['environment']:8} enabled={d['enabled']:5} namespace={d['namespace']}")
    else:
        promote(root, args.product, args.staging_commit, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, FileNotFoundError, subprocess.CalledProcessError) as error:
        sys.exit(f"Environment configuration error: {error}")
