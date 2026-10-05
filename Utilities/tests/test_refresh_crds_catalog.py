import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jsonschema


ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location(
    "refresh_crds_catalog", ROOT / "Utilities/refresh-crds-catalog.py"
)
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


class RefreshTests(unittest.TestCase):
    def test_rebuild_preserves_tooling_and_uses_fixed_converter(self):
        schema = {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "properties": {
                        "properties": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
        }
        snapshot = {"items": [
            {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
             "spec": {"group": "external-secrets.io", "names": {"kind": kind},
                      "versions": [{"name": "v1", "schema": {"openAPIV3Schema": schema}}]}}
            for kind in ("SecretStore", "ClusterSecretStore")
        ]}
        real_run = refresh.run

        def run(*args, **kwargs):
            if args[0] == "kubectl":
                return json.dumps(snapshot)
            return real_run(*args, **kwargs)

        git_config = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "user.name",
            "GIT_CONFIG_VALUE_0": "Catalog tests",
            "GIT_CONFIG_KEY_1": "user.email",
            "GIT_CONFIG_VALUE_1": "catalog-tests@example.invalid",
        }
        with tempfile.TemporaryDirectory(prefix="refresh-test-") as directory, \
                patch.dict(os.environ, git_config):
            root = Path(directory)
            upstream, fork = root / "upstream", root / "fork"
            refresh.run("git", "init", "--initial-branch=main", str(upstream))
            (upstream / "Utilities").mkdir()
            (upstream / "Utilities/openapi2jsonschema.py").write_text(
                (ROOT / "Utilities/openapi2jsonschema.py").read_text()
            )
            refresh.git(upstream, "apply", "--reverse",
                        str(ROOT / "Utilities/patches/schema-properties.patch"))
            converter = upstream / "Utilities/openapi2jsonschema.py"
            converter.write_text(converter.read_text() + '\nUPSTREAM_CHANGE = True\n')
            refresh.git(upstream, "add", ".")
            refresh.git(upstream, "commit", "-m", "Initialize upstream fixture")
            refresh.run("git", "clone", str(upstream), str(fork))
            refresh.git(fork, "remote", "set-url", "origin", str(fork))
            original = refresh.git(fork, "rev-parse", "HEAD")
            recovery = root / "recovery"
            with patch.object(refresh, "FORK", str(fork)), \
                    patch.object(refresh, "UPSTREAM", str(upstream)), \
                    patch.object(refresh, "run", side_effect=run), \
                    patch("sys.argv", ["refresh", "--repo", str(fork),
                                       "--recovery-dir", str(recovery)]):
                refresh.main()
            self.assertEqual(refresh.git(fork, "rev-parse", "HEAD"), original)
            self.assertEqual(refresh.git(fork, "status", "--porcelain"), "")
            bundle = next(recovery.glob("*/prepared.bundle"))
            rebuilt = root / "rebuilt"
            refresh.run("git", "clone", "--branch", "main", str(bundle), str(rebuilt))
            self.assertEqual(refresh.snapshot_tooling(rebuilt), refresh.snapshot_tooling(ROOT))
            self.assertIn("UPSTREAM_CHANGE = True",
                          (rebuilt / "Utilities/openapi2jsonschema.py").read_text())
            for kind in ("secretstore", "clustersecretstore"):
                generated = json.loads((rebuilt / "external-secrets.io" / f"{kind}_v1.json").read_text())
                jsonschema.Draft4Validator.check_schema(generated)
                fields = generated["properties"]["spec"]["properties"]
                self.assertNotIn("additionalProperties", fields)
                self.assertFalse(generated["properties"]["spec"]["additionalProperties"])

    def test_already_applied_patch_preserves_upstream_changes(self):
        with tempfile.TemporaryDirectory(prefix="refresh-test-") as directory:
            checkout = Path(directory)
            refresh.run("git", "init", str(checkout))
            refresh.restore_tooling(checkout, refresh.snapshot_tooling(ROOT))
            converter = checkout / "Utilities/openapi2jsonschema.py"
            content = (ROOT / "Utilities/openapi2jsonschema.py").read_text() + '\nUPSTREAM_CHANGE = True\n'
            converter.write_text(content)
            refresh.patch_converter(checkout)
            self.assertEqual(converter.read_text(), content)

    def test_conflicting_patch_stops_without_overwriting_converter(self):
        with tempfile.TemporaryDirectory(prefix="refresh-test-") as directory:
            checkout = Path(directory)
            refresh.run("git", "init", str(checkout))
            refresh.restore_tooling(checkout, refresh.snapshot_tooling(ROOT))
            converter = checkout / "Utilities/openapi2jsonschema.py"
            content = 'raise RuntimeError("Changed upstream implementation")\n'
            converter.write_text(content)
            with self.assertRaisesRegex(RuntimeError, "Upstream converter conflicts"):
                refresh.patch_converter(checkout)
            self.assertEqual(converter.read_text(), content)

    def test_symlink_destination_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="refresh-test-") as directory:
            root = Path(directory)
            checkout = root / "checkout"
            checkout.mkdir()
            outside = root / "outside"
            outside.mkdir()
            (checkout / "Utilities").symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "symlink tooling destination"):
                refresh.restore_tooling(checkout, {"Utilities/tool.py": (b"", 0o644)})
            self.assertFalse((outside / "tool.py").exists())


if __name__ == "__main__":
    unittest.main()
