import json
import unittest
from pathlib import Path

import jsonschema


ROOT = Path(__file__).parents[2]
META = jsonschema.Draft4Validator(jsonschema.Draft4Validator.META_SCHEMA)


class CatalogTests(unittest.TestCase):
    def test_expected_cluster_groups_are_present(self):
        # Served CRD groups observed in cluster-1 on 2026-11-25.
        expected = {
            "acme.cert-manager.io", "argoproj.io", "cert-manager.io",
            "external-secrets.io", "externaldns.k8s.io", "gateway.networking.k8s.io",
            "generators.external-secrets.io", "helm.cattle.io", "hub.traefik.io",
            "k3s.cattle.io", "longhorn.io", "nfd.k8s-sigs.io", "policy.cert-manager.io",
            "postgres-operator.crunchydata.com", "snapshot.storage.k8s.io", "traefik.io",
            "trust.cert-manager.io", "upgrade.cattle.io",
        }
        actual = {path.name for path in ROOT.iterdir() if path.is_dir()
                  and any(path.glob("*.json"))}
        self.assertFalse(expected - actual, f"Missing catalog groups: {sorted(expected - actual)}")

    def test_every_schema_is_valid_and_named_for_kubeconform(self):
        files = sorted(ROOT.glob("*.*/*.json"))
        self.assertTrue(files, "catalog has no schemas")
        for path in files:
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertRegex(path.parent.name, r"^[a-z0-9.-]+$")
                self.assertRegex(path.name, r"^[a-z][a-z0-9-]*_[a-z][a-z0-9-]*\.json$")
                # Patterns use RE2 syntax such as \\pZ, which Python's format checker rejects.
                META.validate(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
