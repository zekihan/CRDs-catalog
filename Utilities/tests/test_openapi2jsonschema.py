import copy
import importlib.util
import json
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


if __name__ == "__main__":
    unittest.main()
