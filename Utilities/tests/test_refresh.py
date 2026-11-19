import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).parents[2]
spec = importlib.util.spec_from_file_location("refresh", ROOT / "Utilities/refresh.py")
refresh = importlib.util.module_from_spec(spec)
spec.loader.exec_module(refresh)


def crd(group, kind, versions, schema=None):
    schema = schema or {"type": "object", "properties": {"spec": {"type": "object"}}}
    return {"apiVersion": "apiextensions.k8s.io/v1", "kind": "CustomResourceDefinition",
            "metadata": {"name": f"{kind.lower()}s.{group}"},
            "spec": {"group": group, "names": {"kind": kind},
                     "versions": [{"name": v, "schema": {"openAPIV3Schema": schema}}
                                  for v in versions]}}


class RefreshTests(unittest.TestCase):
    def test_writes_group_layout_and_removes_stale_groups(self):
        schema = {"type": "object", "properties": {"spec": {"type": "object", "properties": {
            "properties": {"type": "array", "items": {"type": "string"}}}}}}
        snapshot = {"items": [crd("external-secrets.io", "SecretStore", ["v1", "v1beta1"], schema),
                              crd("traefik.io", "Middleware", ["v1alpha1"])]}
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as work:
            root = Path(directory)
            (root / "stale.example.com").mkdir()
            (root / "stale.example.com/old_v1.json").write_text("{}")
            (root / "Utilities").mkdir()
            (root / ".github").mkdir()
            tree = refresh.convert(snapshot, Path(work))
            refresh.publish(tree, root)
            files = sorted(str(p.relative_to(root)) for p in root.glob("*.*/*.json"))
            self.assertEqual(files, ["external-secrets.io/secretstore_v1.json",
                                     "external-secrets.io/secretstore_v1beta1.json",
                                     "traefik.io/middleware_v1alpha1.json"])
            self.assertTrue((root / "Utilities").is_dir())
            converted = json.loads((root / "external-secrets.io/secretstore_v1.json").read_text())
            jsonschema.Draft4Validator.check_schema(converted)
            jsonschema.validate({"spec": {"properties": ["a"]}}, converted)
            with self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({"spec": {"unknown": True}}, converted)

    def test_preserves_exponent_numbers_in_json_snapshot(self):
        schema = {"type": "object", "properties": {
            "threshold": {"type": "number", "maximum": 1e-6, "default": 1e-7},
        }}
        snapshot = {"items": [crd("example.io", "Widget", ["v1"], schema)]}
        with tempfile.TemporaryDirectory() as work:
            tree = refresh.convert(snapshot, Path(work))
            converted = json.loads((tree / "example.io/widget_v1.json").read_text())
        jsonschema.Draft4Validator.check_schema(converted)
        validator = jsonschema.Draft4Validator(converted)
        validator.validate({"threshold": 1e-7})
        self.assertFalse(validator.is_valid({"threshold": 1e-5}))
        self.assertEqual(converted["properties"]["threshold"]["default"], 1e-7)

    def test_blocked_publish_preserves_existing_catalog(self):
        for blocker in ("file", "dangling symlink"):
            with self.subTest(blocker=blocker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                utilities = root / "Utilities"
                utilities.mkdir()
                for script in ("refresh.py", "openapi2jsonschema.py"):
                    shutil.copyfile(ROOT / "Utilities" / script, utilities / script)
                group = root / "current.example"
                group.mkdir()
                old_schema = group / "widget_v1.json"
                original = '{"description": "old schema"}\n'
                old_schema.write_text(original)
                destination = root / "unavailable.example"
                if blocker == "file":
                    destination.write_text("keep me")
                else:
                    destination.symlink_to(root / "missing")
                snapshot = root / "input.json"
                snapshot.write_text(json.dumps({"items": [
                    crd("current.example", "Widget", ["v2"]),
                    crd("unavailable.example", "Widget", ["v1"]),
                ]}))
                result = subprocess.run(
                    [sys.executable, str(utilities / "refresh.py"), "--input", str(snapshot)],
                    cwd=root, capture_output=True, text=True, timeout=30, check=False,
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertEqual(list(group.iterdir()), [old_schema], result.stderr)
                self.assertEqual(old_schema.read_text(), original)
                self.assertEqual(result.stderr.strip(),
                                 f"Stopped: Refusing to overwrite schema destination: {destination}")

    def test_exports_only_served_versions_with_their_own_schemas(self):
        item = crd("example.io", "Widget", ["v1beta1", "v1", "v2"])
        retired, stable, newer = item["spec"]["versions"]
        retired["served"] = False
        for version, value_type in ((stable, "string"), (newer, "integer")):
            version["served"] = True
            version["schema"]["openAPIV3Schema"] = {
                "type": "object", "properties": {"value": {"type": value_type}},
            }
        with tempfile.TemporaryDirectory() as work:
            group = refresh.convert({"items": [item]}, Path(work)) / "example.io"
            self.assertEqual({path.name for path in group.iterdir()},
                             {"widget_v1.json", "widget_v2.json"})
            for name, valid, invalid in (("v1", "five", 5), ("v2", 5, "five")):
                with self.subTest(version=name):
                    schema = json.loads((group / f"widget_{name}.json").read_text())
                    validator = jsonschema.Draft4Validator(schema)
                    validator.validate({"value": valid})
                    self.assertFalse(validator.is_valid({"value": invalid}))

    def test_rejects_snapshot_with_no_served_versions(self):
        item = crd("example.io", "Widget", ["v1"])
        item["spec"]["versions"][0]["served"] = False
        with self.assertRaisesRegex(RuntimeError, "No served CRD versions"):
            refresh.expected_schemas({"items": [item]})

    def test_converts_explicitly_nullable_schema_types(self):
        literal = {"type": "string", "nullable": True}
        schema = {"type": "object", "required": ["labels"], "properties": {
            "labels": {"type": "object", "nullable": True,
                       "additionalProperties": {"type": "string"}},
            "ports": {"type": "array", "items": {
                "type": "string", "format": "int-or-string", "nullable": True,
                "enum": [None, 80, "http"],
            }},
            "name": {"type": "string", "nullable": False},
            "payload": {"type": "object", "default": literal},
        }}
        snapshot = {"items": [crd("example.io", "Widget", ["v1"], schema)]}
        with tempfile.TemporaryDirectory() as work:
            tree = refresh.convert(snapshot, Path(work))
            converted = json.loads((tree / "example.io/widget_v1.json").read_text())
        jsonschema.Draft4Validator.check_schema(converted)
        validator = jsonschema.Draft4Validator(converted)
        validator.validate({"labels": None, "ports": [None, 80, "http"]})
        validator.validate({"labels": {"tier": "backup"}, "name": "example"})
        for instance in ({}, {"labels": {"tier": 1}}, {"labels": None, "name": None},
                         {"labels": None, "ports": [81]}):
            with self.subTest(instance=instance):
                self.assertFalse(validator.is_valid(instance))
        self.assertEqual(converted["properties"]["payload"]["default"], literal)

    def test_preserves_unknown_fields_alongside_declared_properties(self):
        schema = {"type": "object", "properties": {
            "config": {"type": "object", "x-kubernetes-preserve-unknown-fields": True,
                       "properties": {
                           "spec": {"type": "object", "properties": {"mode": {"type": "string"}}},
                           "labels": {"type": "object", "additionalProperties": {"type": "string"}},
                       }},
        }}
        snapshot = {"items": [crd("example.io", "Widget", ["v1"], schema)]}
        with tempfile.TemporaryDirectory() as work:
            tree = refresh.convert(snapshot, Path(work))
            converted = json.loads((tree / "example.io/widget_v1.json").read_text())
        jsonschema.Draft4Validator.check_schema(converted)
        validator = jsonschema.Draft4Validator(converted)
        validator.validate({"config": {"spec": {"mode": "active"}, "labels": {"tier": "test"},
                                       "extension": {"arbitrary": [1, True]}}})
        self.assertFalse(validator.is_valid({"config": {"spec": {"unexpected": True}}}))
        self.assertFalse(validator.is_valid({"config": {"labels": {"tier": 1}}}))

    def test_rejects_empty_and_unsafe_input(self):
        with self.assertRaisesRegex(RuntimeError, "no CRDs"):
            refresh.expected_schemas({"items": []})
        with self.assertRaisesRegex(RuntimeError, "Unsafe group"):
            refresh.expected_schemas({"items": [crd("../evil", "Kind", ["v1"])]})
        with self.assertRaisesRegex(RuntimeError, "Unsafe version"):
            refresh.expected_schemas({"items": [crd("a.io", "Kind", ["v1/../x"])]})


if __name__ == "__main__":
    unittest.main()
