import json
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).parents[2]
META = jsonschema.Draft4Validator(jsonschema.Draft4Validator.META_SCHEMA)


class CatalogTests(unittest.TestCase):
    def test_every_schema_is_valid_and_named_for_kubeconform(self):
        files = sorted(ROOT.glob("*.*/*.json"))
        self.assertTrue(files, "catalog has no schemas")
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertRegex(path.parent.name, r"^[a-z0-9.-]+$")
                self.assertRegex(path.name, r"^[a-z0-9]+_[a-z0-9]+\.json$")
                # Patterns use RE2 syntax such as \\pZ, which Python's format checker rejects.
                META.validate(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
