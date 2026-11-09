# CRDs-catalog

JSON schemas for the CustomResourceDefinitions installed in the
[homelab](https://github.com/zekihan/homelab) cluster, laid out for
[kubeconform](https://github.com/yannh/kubeconform):

```
<group>/<kind>_<version>.json
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

`refresh.py` replaces every group directory with the cluster's current CRDs,
so removed CRDs disappear too. `--input crds.json` converts a saved
`kubectl get crds -o json` instead of reading the cluster.

`Utilities/openapi2jsonschema.py` comes from kubeconform through
datreeio/CRDs-catalog, with a fix that only descends into subschemas, so
defaults, enums and properties named `properties` convert correctly.
