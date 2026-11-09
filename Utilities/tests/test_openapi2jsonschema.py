import copy
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import jsonschema


spec = importlib.util.spec_from_file_location(
    "openapi2jsonschema", Path(__file__).parents[1] / "openapi2jsonschema.py"
)
converter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(converter)


class AdditionalPropertiesTests(unittest.TestCase):
    def test_property_named_properties_remains_valid(self):
        schema = {
            "type": "object",
            "properties": {
                "rules": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "properties": {"type": "array", "items": {"type": "string"}},
                            "additionalProperties": {"type": "boolean"},
                        },
                    },
                },
            },
        }
        converted = converter.additional_properties(copy.deepcopy(schema), skip=True)
        jsonschema.Draft4Validator.check_schema(converted)
        validator = jsonschema.Draft4Validator(converted)
        validator.validate({"rules": [{"properties": ["spec.secretValue"],
                                       "additionalProperties": True}]})
        with self.assertRaises(jsonschema.ValidationError):
            validator.validate({"rules": [{"unexpected": True}]})
        self.assertNotIn("additionalProperties", converted)

    def test_schema_maps_and_combinators(self):
        for keyword in ("properties", "patternProperties", "definitions", "$defs"):
            with self.subTest(keyword=keyword):
                schema = {keyword: {"properties": {"type": "object", "properties": {}}}}
                converted = converter.additional_properties(schema, skip=True)
                self.assertNotIn("additionalProperties", converted[keyword])
                self.assertFalse(converted[keyword]["properties"]["additionalProperties"])
        for keyword in ("allOf", "anyOf", "oneOf", "items"):
            with self.subTest(keyword=keyword):
                schema = {keyword: [{"type": "object", "properties": {}}]}
                converted = converter.additional_properties(schema)
                self.assertFalse(converted[keyword][0]["additionalProperties"])

    def test_explicit_additional_properties_is_preserved(self):
        schema = {"type": "object", "properties": {}, "additionalProperties": True}
        self.assertEqual(converter.additional_properties(copy.deepcopy(schema)), schema)

    def test_secret_store_catalog_schemas_compile(self):
        root = Path(__file__).parents[2]
        for kind in ("secretstore", "clustersecretstore"):
            with self.subTest(kind=kind):
                schema = json.loads((root / "external-secrets.io" / f"{kind}_v1.json").read_text())
                jsonschema.Draft4Validator.check_schema(schema)


class SchemaTransformationTests(unittest.TestCase):
    def test_literal_objects_are_preserved(self):
        literal = {"properties": {"port": {"format": "int-or-string"}}}
        schema = {
            "type": "object",
            "properties": {"payload": {
                "type": "object", "additionalProperties": True,
                "default": copy.deepcopy(literal), "enum": [copy.deepcopy(literal)],
                "example": copy.deepcopy(literal),
            }},
        }
        for transform in (converter.additional_properties, converter.replace_int_or_string):
            with self.subTest(transform=transform.__name__):
                converted = transform(copy.deepcopy(schema))
                payload = converted["properties"]["payload"]
                self.assertEqual(payload["default"], literal)
                self.assertEqual(payload["enum"], [literal])
                self.assertEqual(payload["example"], literal)
                jsonschema.Draft4Validator(converted).validate({"payload": literal})

    def test_int_or_string_preserves_constraints(self):
        field = {"type": "string", "format": "int-or-string",
                 "enum": [80, "http"], "description": "A supported port", "default": 80}
        schema = {"type": "object", "properties": {"port": field}}
        converted = converter.replace_int_or_string(schema)
        validator = jsonschema.Draft4Validator(converted)
        for value in (80, "http"):
            validator.validate({"port": value})
        for value in (81, "https", True, 1.5):
            with self.subTest(value=value), self.assertRaises(jsonschema.ValidationError):
                validator.validate({"port": value})
        self.assertEqual(converted["properties"]["port"]["description"], field["description"])
        self.assertEqual(converted["properties"]["port"]["default"], 80)
        self.assertEqual(schema["properties"]["port"], field)

    def test_int_or_string_in_schema_positions(self):
        field = {"format": "int-or-string", "minimum": 2, "minLength": 2}
        schemas = [field, {"items": field}, {"oneOf": [field]},
                   {"allOf": [field]}, {"anyOf": [field]},
                   {"additionalProperties": field},
                   {"properties": {"format": field}},
                   {"definitions": {"port": field}, "$ref": "#/definitions/port"},
                   {"patternProperties": {".*": field}}]
        for schema in schemas:
            with self.subTest(schema=schema):
                converted = converter.replace_int_or_string(copy.deepcopy(schema))
                jsonschema.Draft4Validator.check_schema(converted)
                if "items" in schema:
                    valid, invalid = [2], [1]
                elif "additionalProperties" in schema:
                    valid, invalid = {"port": 2}, {"port": 1}
                elif "properties" in schema:
                    valid, invalid = {"format": 2}, {"format": 1}
                elif "patternProperties" in schema:
                    valid, invalid = {"port": 2}, {"port": 1}
                else:
                    valid, invalid = 2, 1
                validator = jsonschema.Draft4Validator(converted)
                validator.validate(valid)
                if schema is schemas[0]:
                    validator.validate("http")
                    self.assertFalse(validator.is_valid(True))
                with self.assertRaises(jsonschema.ValidationError):
                    validator.validate(invalid)

    def test_cli_preserves_constraints_and_literals(self):
        literal = {"properties": {"format": "int-or-string"}}
        schema = {"type": "object", "properties": {
            "port": {"format": "int-or-string", "enum": [80, "http"]},
            "payload": {"type": "object", "enum": [literal], "default": literal},
        }}
        crd = {"kind": "CustomResourceDefinition", "spec": {
            "group": "example.io", "names": {"kind": "Widget"},
            "versions": [{"name": "v1", "schema": {"openAPIV3Schema": schema}}],
        }}
        with tempfile.TemporaryDirectory(prefix="converter-test-") as directory:
            source = Path(directory) / "crd.json"
            source.write_text(json.dumps(crd))
            env = os.environ.copy()
            env.pop("FILENAME_FORMAT", None)
            subprocess.run([sys.executable, str(Path(converter.__file__).resolve()), str(source)],
                           cwd=directory, env=env, check=True, capture_output=True, text=True)
            converted = json.loads((Path(directory) / "widget_v1.json").read_text())
        jsonschema.Draft4Validator.check_schema(converted)
        validator = jsonschema.Draft4Validator(converted)
        validator.validate({"port": 80, "payload": literal})
        self.assertFalse(validator.is_valid({"port": 81}))
        self.assertEqual(converted["properties"]["payload"]["default"], literal)

    def test_property_named_format_is_not_a_format_keyword(self):
        schema = {"properties": {"format": {"format": "int-or-string"},
                                 "name": {"type": "string"}}}
        converted = converter.replace_int_or_string(copy.deepcopy(schema))
        self.assertEqual(set(converted["properties"]), {"format", "name"})
        jsonschema.Draft4Validator(converted).validate({"format": 80, "name": "web"})


class OutputDiscoveryTests(unittest.TestCase):
    def crd(self, group, kind="Widget", versions=("v1",)):
        return {"kind": "CustomResourceDefinition", "spec": {
            "group": group, "names": {"kind": kind},
            "versions": [{"name": version, "schema": {"openAPIV3Schema": {
                "type": "object", "description": group,
            }}} for version in versions],
        }}

    def run_converter(self, root, documents, filename_format=None):
        sources = []
        for i, document in enumerate(documents):
            source = root / f"input-{i}.json"
            source.write_text(json.dumps(document))
            sources.append(str(source))
        env = os.environ.copy()
        env.pop("FILENAME_FORMAT", None)
        if filename_format is not None:
            env["FILENAME_FORMAT"] = filename_format
        return subprocess.run(
            [sys.executable, str(Path(converter.__file__).resolve()), *sources],
            cwd=root, env=env, capture_output=True, text=True, timeout=30,
        )

    def test_colliding_outputs_fail_before_publishing(self):
        first = self.crd("first.io")
        second = self.crd("second.io")
        cases = [
            ([first, second], None),
            ([{"kind": "List", "items": [first, second]}], None),
            ([self.crd("first.io", versions=("v1", "v1"))], None),
            # The collision check must use the basename actually written.
            ([first, second], "{fullgroup}/{kind}_{version}"),
        ]
        for documents, filename_format in cases:
            with self.subTest(documents=documents, filename_format=filename_format), \
                    tempfile.TemporaryDirectory(prefix="converter-collision-") as directory:
                root = Path(directory)
                output = root / "widget_v1.json"
                previous = b'{"description": "previous schema"}\n'
                output.write_bytes(previous)
                # Even an earlier, distinct output must not publish on failure.
                result = self.run_converter(
                    root, [self.crd("unique.io", kind="Other"), *documents], filename_format)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("Duplicate output filename", result.stderr)
                self.assertIn("widget_v1.json", result.stderr)
                self.assertEqual(output.read_bytes(), previous)
                self.assertFalse((root / "other_v1.json").exists())

    def test_fullgroup_format_keeps_distinct_schemas(self):
        with tempfile.TemporaryDirectory(prefix="converter-distinct-") as directory:
            root = Path(directory)
            result = self.run_converter(
                root, [self.crd(group, versions=("v1", "v2"))
                       for group in ("first.io", "second.io")],
                "{fullgroup}_{kind}_{version}",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            for group in ("first.io", "second.io"):
                for version in ("v1", "v2"):
                    output = json.loads((root / f"{group}_widget_{version}.json").read_text())
                    self.assertEqual(output["description"], group)


if __name__ == "__main__":
    unittest.main()
