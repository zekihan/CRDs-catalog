import importlib.util
import json
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

    def test_rejects_empty_and_unsafe_input(self):
        with self.assertRaisesRegex(RuntimeError, "no CRDs"):
            refresh.expected_schemas({"items": []})
        with self.assertRaisesRegex(RuntimeError, "Unsafe group"):
            refresh.expected_schemas({"items": [crd("../evil", "Kind", ["v1"])]})
        with self.assertRaisesRegex(RuntimeError, "Unsafe version"):
            refresh.expected_schemas({"items": [crd("a.io", "Kind", ["v1/../x"])]})


if __name__ == "__main__":
    unittest.main()
