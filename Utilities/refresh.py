#!/usr/bin/env python3
"""Export JSON schemas for every CRD installed in a cluster.

Writes <group>/<kind>_<version>.json for each served CRD version and removes
schema directories the cluster no longer has. Review with git diff, then commit.
Requires kubectl and PyYAML.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONVERTER = ROOT / "Utilities/openapi2jsonschema.py"


def schema_dirs(root):
    """Top-level API group directories; every CRD group contains a dot."""
    return {path for path in root.iterdir()
            if path.is_dir() and not path.name.startswith(".") and "." in path.name}


def expected_schemas(snapshot):
    items = snapshot.get("items", [])
    if not items:
        raise RuntimeError("Cluster returned no CRDs; refusing an empty export.")
    expected, clean = set(), []
    for item in items:
        spec = item["spec"]
        group, kind = spec["group"], spec["names"]["kind"]
        if not re.fullmatch(r"[a-z0-9.-]+", group) or "." not in group:
            raise RuntimeError(f"Unsafe group: {group}")
        if not re.fullmatch(r"[A-Za-z0-9]+", kind):
            raise RuntimeError(f"Unsafe kind: {kind}")
        for version in spec["versions"]:
            name = version["name"]
            if not re.fullmatch(r"[a-z0-9]+", name):
                raise RuntimeError(f"Unsafe version: {name}")
            if "openAPIV3Schema" not in version.get("schema", {}):
                raise RuntimeError(f"Missing schema: {group}/{kind}/{name}")
            filename = f"{group}_{kind}_{name}.json".lower()
            if filename in expected:
                raise RuntimeError(f"Duplicate schema: {filename}")
            expected.add(filename)
        clean.append({"apiVersion": item["apiVersion"],
                      "kind": "CustomResourceDefinition", "spec": spec})
    return expected, {"items": clean}


def convert(snapshot, workdir):
    expected, clean = expected_schemas(snapshot)
    source = workdir / "crds.json"
    source.write_text(json.dumps(clean))
    converted = workdir / "converted"
    converted.mkdir()
    env = {key: value for key, value in os.environ.items()
           if key not in ("DENY_ROOT_ADDITIONAL_PROPERTIES", "DISABLE_SSL_CERT_VALIDATION")}
    env["FILENAME_FORMAT"] = "{fullgroup}_{kind}_{version}"
    subprocess.run([sys.executable, str(CONVERTER), str(source)],
                   cwd=converted, env=env, check=True, stdout=subprocess.DEVNULL)
    actual = {path.name for path in converted.iterdir()}
    if actual != expected:
        raise RuntimeError(f"Incomplete conversion: missing {sorted(expected - actual)}, "
                           f"extra {sorted(actual - expected)}")
    tree = workdir / "tree"
    for filename in sorted(expected):
        path = converted / filename
        schema = json.loads(path.read_text())
        if not isinstance(schema, dict) or not schema:
            raise RuntimeError(f"Invalid schema: {filename}")
        group, remainder = filename.split("_", 1)
        destination = tree / group / remainder
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, destination)
    return tree


def publish(tree, root):
    for directory in schema_dirs(root):
        if directory.is_symlink():
            raise RuntimeError(f"Refusing symlink schema directory: {directory}")
        shutil.rmtree(directory)
    for directory in sorted(tree.iterdir()):
        shutil.copytree(directory, root / directory.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", help="kubectl context (default: current context)")
    parser.add_argument("--input", type=Path,
                        help="read `kubectl get crds -o json` output from a file instead")
    args = parser.parse_args()
    if args.input:
        snapshot = json.loads(args.input.read_text())
    else:
        command = ["kubectl", "--request-timeout=60s", "get", "crds", "-o", "json"]
        if args.context:
            command[1:1] = ["--context", args.context]
        snapshot = json.loads(subprocess.run(command, check=True, text=True,
                                             stdout=subprocess.PIPE).stdout)
    with tempfile.TemporaryDirectory(prefix="crds-catalog-") as directory:
        tree = convert(snapshot, Path(directory))
        publish(tree, ROOT)
    print(f"Wrote {sum(1 for _ in ROOT.glob('*.*/*.json'))} schemas; review with git diff.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as error:
        sys.exit(f"Stopped: {error}")
