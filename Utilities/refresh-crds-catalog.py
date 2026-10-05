#!/usr/bin/env python3
"""Replace fork-only main history with upstream plus live CRD schemas.

Install PyYAML in the Python environment running this script first.
Default mode prepares a reviewable result without changing either main branch.
Use --apply to publish it and reset the clean local main branch.
The refresh tooling is preserved and the converter fix is patched onto upstream.
Recovery bundles and the review patch live outside the repo.
"""

import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FORK = "git@github.com:zekihan/CRDs-catalog.git"
UPSTREAM = "https://github.com/datreeio/CRDs-catalog.git"
PRESERVED_FILES = (
    "Utilities/refresh-crds-catalog.py",
    "Utilities/patches/schema-properties.patch",
    "Utilities/tests/test_openapi2jsonschema.py",
    "Utilities/tests/test_refresh_crds_catalog.py",
    ".github/workflows/crd-policy-tests.yml",
    ".github/scripts/requirements.txt",
)


def snapshot_tooling(root):
    root = root.resolve()
    snapshot = {}
    for filename in PRESERVED_FILES:
        source = root / filename
        if source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
            raise RuntimeError(f"Refusing symlink tooling source: {source}")
        snapshot[filename] = (source.read_bytes(), source.stat().st_mode & 0o777)
    return snapshot


def restore_tooling(checkout, snapshot):
    checkout = checkout.resolve()
    for filename, (content, mode) in snapshot.items():
        destination = checkout / filename
        if destination.is_symlink() or any(parent.is_symlink() for parent in destination.parents):
            raise RuntimeError(f"Refusing symlink tooling destination: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
        destination.chmod(mode)
        if destination.suffix == ".py":
            run(sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())",
                str(destination))


def run(*args, cwd=None, env=None):
    return subprocess.run(args, cwd=cwd, env=env, check=True,
                          text=True, stdout=subprocess.PIPE).stdout.strip()


def git(repo, *args):
    return run("git", "--no-optional-locks", "-c", "core.fsmonitor=false",
               "-C", str(repo), *args)


def patch_converter(checkout):
    checkout = checkout.resolve()
    converter = checkout / "Utilities/openapi2jsonschema.py"
    if converter.is_symlink() or converter.parent.is_symlink():
        raise RuntimeError("Refusing symlink converter destination.")
    patch_file = checkout / "Utilities/patches/schema-properties.patch"
    try:
        git(checkout, "apply", "--check", str(patch_file))
    except subprocess.CalledProcessError:
        try:
            git(checkout, "apply", "--reverse", "--check", str(patch_file))
        except subprocess.CalledProcessError as error:
            raise RuntimeError("Upstream converter conflicts with schema-properties.patch; "
                               "review and update the patch before rebuilding.") from error
        return
    git(checkout, "apply", str(patch_file))


def check_local(repo, original=None):
    if git(repo, "branch", "--show-current") != "main":
        raise RuntimeError("Local checkout must be on main.")
    if git(repo, "status", "--porcelain=v1", "--untracked-files=all"):
        raise RuntimeError("Local checkout has uncommitted files; preserve them first.")
    head = git(repo, "rev-parse", "HEAD")
    if original is not None and head != original:
        raise RuntimeError("Local main changed during this run; refusing to reset it.")
    return head


def schemas_expected(snapshot):
    items = snapshot.get("items", [])
    if not items:
        raise RuntimeError("Cluster returned no CRDs; refusing an empty export.")
    expected = set()
    clean = []
    for item in items:
        spec = item["spec"]
        group, kind = spec["group"], spec["names"]["kind"]
        if not re.fullmatch(r"[a-z0-9.-]+", group):
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
    if not expected:
        raise RuntimeError("No versioned schemas found.")
    return expected, {"items": clean}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path,
                        default=Path("/Users/zekihan/repos/github.com/zekihan/CRDs-catalog"))
    parser.add_argument("--context", default="cluster-1-local")
    parser.add_argument("--recovery-dir", type=Path,
                        default=Path.home() / "Documents/Codex/crds-catalog-recovery")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        import yaml  # noqa: F401
    except ImportError:
        parser.error("PyYAML is required: create a virtual environment and install pyyaml.")
    repo = args.repo.resolve()
    script = Path(__file__).resolve()
    tooling = snapshot_tooling(script.parent.parent)
    recovery_root = args.recovery_dir.resolve()
    if recovery_root.is_relative_to(repo):
        raise RuntimeError("Recovery directory must be outside the catalog repository.")
    original = check_local(repo)
    urls = git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()
    allowed = {FORK, "https://github.com/zekihan/CRDs-catalog.git",
               "https://github.com/zekihan/CRDs-catalog"}
    if len(urls) != 1 or urls[0] not in allowed:
        raise RuntimeError("Origin must push only to zekihan/CRDs-catalog.")
    snapshot = json.loads(run("kubectl", "--context", args.context,
                              "--request-timeout=60s", "get", "crds", "-o", "json"))
    expected, clean = schemas_expected(snapshot)
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    recovery_root.mkdir(parents=True, exist_ok=True)
    recovery = Path(tempfile.mkdtemp(prefix=f"crds-recovery-{stamp}-", dir=recovery_root))
    git(repo, "bundle", "create", str(recovery / "local.bundle"), "--all")
    git(repo, "bundle", "verify", str(recovery / "local.bundle"))
    print(f"Recovery and review files: {recovery}", flush=True)
    with tempfile.TemporaryDirectory(prefix="refresh-crds-") as temporary:
        root = Path(temporary)
        checkout = root / "catalog"
        run("git", "clone", "--no-checkout", "--single-branch", "--branch", "main",
            FORK, str(checkout))
        old_fork = git(checkout, "rev-parse", "refs/remotes/origin/main")
        git(checkout, "bundle", "create", str(recovery / "fork.bundle"), "--all")
        git(checkout, "bundle", "verify", str(recovery / "fork.bundle"))
        git(checkout, "fetch", UPSTREAM, "refs/heads/main")
        upstream_head = git(checkout, "rev-parse", "FETCH_HEAD")
        git(checkout, "checkout", "-B", "main", upstream_head)
        restore_tooling(checkout, tooling)
        patch_converter(checkout)
        git(checkout, "add", *tooling, "Utilities/openapi2jsonschema.py")
        git(checkout, "diff", "--cached", "--check")
        if git(checkout, "diff", "--cached", "--name-only"):
            git(checkout, "commit", "-m", "Preserve catalog refresh tooling")
        source = root / "crds.json"
        source.write_text(json.dumps(clean))
        converted = root / "converted"
        converted.mkdir()
        env = os.environ.copy()
        for key in ("DENY_ROOT_ADDITIONAL_PROPERTIES", "DISABLE_SSL_CERT_VALIDATION"):
            env.pop(key, None)
        env["FILENAME_FORMAT"] = "{fullgroup}_{kind}_{version}"
        run(sys.executable, str(checkout / "Utilities/openapi2jsonschema.py"),
            str(source), cwd=converted, env=env)
        actual = {path.name for path in converted.iterdir()}
        if actual != expected:
            raise RuntimeError(f"Incomplete conversion: missing {expected - actual}, extra {actual - expected}")
        for filename in sorted(expected):
            path = converted / filename
            schema = json.loads(path.read_text())
            if not isinstance(schema, dict) or not schema:
                raise RuntimeError(f"Invalid schema: {filename}")
            group, remainder = filename.split("_", 1)
            destination = checkout / group / remainder
            if destination.parent.is_symlink() or destination.is_symlink():
                raise RuntimeError(f"Refusing symlink destination: {destination}")
            destination.parent.mkdir(exist_ok=True)
            shutil.copyfile(path, destination)
        git(checkout, "add", "--all")
        git(checkout, "diff", "--cached", "--check")
        patch = git(checkout, "diff", "--cached", "--binary")
        (recovery / "schemas.patch").write_text(patch + "\n" if patch else "")
        summary = git(checkout, "diff", "--cached", "--stat")
        print(summary or "Live schemas already match upstream.", flush=True)
        if patch:
            git(checkout, "commit", "-m", "Update schemas from live cluster")
        new_head = git(checkout, "rev-parse", "HEAD")
        git(checkout, "bundle", "create", str(recovery / "prepared.bundle"), "main")
        (recovery / "heads.json").write_text(json.dumps({
            "local": original, "fork": old_fork, "upstream": upstream_head,
            "prepared": new_head, "context": args.context}, indent=2) + "\n")
        if not args.apply:
            print("Prepared only. Run again with --apply to replace fork main and local main.")
            return
        check_local(repo, original)
        git(checkout, "push", f"--force-with-lease=refs/heads/main:{old_fork}",
            "origin", "HEAD:refs/heads/main")
        remote_head = git(checkout, "ls-remote", "origin", "refs/heads/main").split()[0]
        if remote_head != new_head:
            raise RuntimeError("Fork main changed after push; local checkout was not reset.")
        check_local(repo, original)
        git(repo, "fetch", str(recovery / "prepared.bundle"), "main")
        git(repo, "reset", "--hard", new_head)
        git(repo, "fetch", "origin", "+refs/heads/main:refs/remotes/origin/main")
        print(f"Published and updated local main: {new_head}")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, KeyError, ValueError) as error:
        sys.exit(f"Stopped: {error}")
