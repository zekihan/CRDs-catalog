import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


SCRIPT = Path(__file__).parents[1] / "auto-update-index.py"
spec = importlib.util.spec_from_file_location("auto_update_index", SCRIPT)
indexer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(indexer)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="index-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "catalog"
        self.group = self.source / "example.io"
        self.group.mkdir(parents=True)
        self.index = self.root / "index.yaml"
        self.schema("thing_v1.json")

    def schema(self, name, data=None, group=None):
        path = (group or self.group) / name
        path.write_text(json.dumps(data if data is not None else {
            "type": "object", "description": "Thing resource",
            "properties": {"spec": {"type": "object"}},
        }))
        return path

    def run_index(self, source=None, index=None, cwd=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(source or self.source),
             str(index or self.index)], cwd=cwd or self.root,
            capture_output=True, text=True, timeout=30,
        )

    def assert_failed_preserving_index(self, result, previous):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn("Index update failed", result.stderr)
        self.assertEqual(self.index.read_bytes(), previous)
        self.assertEqual(list(self.root.glob(".index.yaml.*.tmp")), [])

    def test_absolute_dot_relative_and_parent_sources_match(self):
        expected = None
        for source, cwd in ((self.source, self.root), ("catalog", self.root),
                            ("./catalog", self.root), ("../catalog", self.root),
                            (".", self.source)):
            # ../catalog from a sibling directory exercises parent components.
            if source == "../catalog":
                cwd = self.root / "sibling"
                cwd.mkdir()
            with self.subTest(source=source):
                output = self.root / ("output" + str(len(list(self.root.glob('output*')))) + ".yaml")
                result = self.run_index(source, output, cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                content = output.read_bytes()
                expected = content if expected is None else expected
                self.assertEqual(content, expected)
                entries = yaml.safe_load(content)["example.io"]
                self.assertEqual(entries[0]["filename"], "example.io/thing_v1.json")

    def test_incremental_skip_add_and_prune_across_source_spellings(self):
        self.assertEqual(self.run_index().returncode, 0)
        previous = self.index.read_bytes()
        result = self.run_index("catalog")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipped 1 files", result.stdout)
        self.assertIn("Added 0 files", result.stdout)
        self.assertEqual(self.index.read_bytes(), previous)
        (self.group / "thing_v1.json").unlink()
        self.schema("other_v2.json")
        result = self.run_index(".", cwd=self.source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Removed 1 files", result.stdout)
        entries = yaml.safe_load(self.index.read_text())["example.io"]
        self.assertEqual([entry["filename"] for entry in entries], ["example.io/other_v2.json"])

    def test_invalid_new_schema_prevents_partial_publish(self):
        self.assertEqual(self.run_index().returncode, 0)
        previous = self.index.read_bytes()
        good_group = self.source / "another.io"
        good_group.mkdir()
        self.schema("other_v1.json", group=good_group)
        (self.group / "broken_v1.json").write_text('{broken: [')
        self.assert_failed_preserving_index(self.run_index(), previous)

    def test_malformed_schema_shapes_and_names_fail(self):
        invalid = [[], "schema", False, 42, {"description": 3},
                   {"description": None}, {"description": []}]
        for data in invalid:
            with self.subTest(data=data):
                self.index.write_bytes(b"# Previous index\n{}\n")
                previous = self.index.read_bytes()
                self.schema("broken_v1.json", data)
                self.assert_failed_preserving_index(self.run_index(), previous)
        (self.group / "broken_v1.json").unlink()
        for name in ("invalid.json", "_v1.json", "thing_.json"):
            with self.subTest(name=name):
                path = self.schema(name)
                self.assert_failed_preserving_index(self.run_index(), previous)
                path.unlink()

    def test_invalid_indexed_schema_is_not_hidden_by_skip(self):
        self.assertEqual(self.run_index().returncode, 0)
        previous = self.index.read_bytes()
        (self.group / "thing_v1.json").write_text('null')
        self.assert_failed_preserving_index(self.run_index(), previous)

    def test_unreadable_schema_aborts_run(self):
        # A mocked open failure is deterministic even under root; main still
        # exercises future exception handling and the publish guard.
        self.assertEqual(self.run_index().returncode, 0)
        previous = self.index.read_bytes()
        with patch.object(indexer, "GetFileDescription", side_effect=PermissionError("unreadable")), \
                patch.object(indexer, "ProcessPoolExecutor",
                             ThreadPoolExecutor), \
                patch.object(sys, "argv", [str(SCRIPT), str(self.source), str(self.index)]):
            with self.assertRaisesRegex(ValueError, "unreadable"):
                indexer.main()
        self.assertEqual(self.index.read_bytes(), previous)

    def test_first_failed_run_does_not_create_index(self):
        (self.group / "broken_v1.json").write_text('{')
        self.assertNotEqual(self.run_index().returncode, 0)
        self.assertFalse(self.index.exists())

    def test_extensions_and_tooling_are_compatible(self):
        self.schema("extensions_v1.json", {
            "description": "Extensions resource",
            "properties": {"spec": {"properties": {
                "additionalProperties": False,
                "field": {"type": "string", "pattern": r"^\PC*$"},
            }}},
        })
        tooling = self.source / "Utilities"
        tooling.mkdir()
        (tooling / "devbox.json").write_text('{"packages": []}')
        result = self.run_index()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(yaml.safe_load(self.index.read_text())["example.io"]), 2)

    def test_publish_mode_is_readable_and_existing_mode_is_preserved(self):
        data = indexer.YamlDataStructure()
        data.save_to_yaml(str(self.index))
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o644)
        self.index.chmod(0o640)
        data.save_to_yaml(str(self.index))
        self.assertEqual(self.index.stat().st_mode & 0o777, 0o640)

    def test_atomic_publish_failure_preserves_bytes_and_cleans_temporary(self):
        self.index.write_bytes(b"# Preserve formatting\n{}\n")
        previous = self.index.read_bytes()
        data = indexer.YamlDataStructure()
        for operation in ("os.replace", "os.fsync", "yaml.dump"):
            with self.subTest(operation=operation), \
                    patch.object(indexer.os if operation.startswith("os") else indexer.yaml,
                                 operation.split('.')[1], side_effect=OSError("injected failure")):
                with self.assertRaises(OSError):
                    data.save_to_yaml(str(self.index))
                self.assertEqual(self.index.read_bytes(), previous)
                self.assertEqual(list(self.root.glob(".index.yaml.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
