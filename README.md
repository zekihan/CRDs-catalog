# CRDs-catalog

JSON schemas for the CustomResourceDefinitions installed in the
[homelab](https://github.com/zekihan/homelab) cluster, laid out for
[kubeconform](https://github.com/yannh/kubeconform):

```
<group>/<lowercase-kind>_<lowercase-version>.json
```

Only CRDs that run in the cluster live here. Homelab CI lists this catalog
before [datreeio/CRDs-catalog](https://github.com/datreeio/CRDs-catalog), so a
schema missing here falls back to upstream:

```sh
kubeconform \
  -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/zekihan/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
  manifests.yaml
```

## Refresh

```sh
python3 -m venv .venv
.venv/bin/pip install --require-hashes --only-binary=:all: -r requirements.txt
.venv/bin/python Utilities/refresh.py --context cluster-1
git diff --stat
.venv/bin/python -m unittest discover -s Utilities/tests
```

`refresh.py` exports served CRD versions and replaces every non-hidden top-level
directory whose name contains a dot, so removed CRDs disappear too. Do not keep
unrelated files in those directories. `--input crds.json` reads a saved
`kubectl get crds -o json` instead of contacting the cluster; it still replaces
the catalog directories. An empty snapshot, a missing schema, or a duplicate
output name stops the export before publication. Publication itself removes the
old directories before copying the replacements.

`Utilities/openapi2jsonschema.py` comes from kubeconform through
datreeio/CRDs-catalog, with a fix that only descends into subschemas, so
defaults, enums and properties named `properties` convert correctly.
