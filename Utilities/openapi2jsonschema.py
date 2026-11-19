#!/usr/bin/env python3

# Copied from https://github.com/yannh/kubeconform/blob/master/scripts/openapi2jsonschema.py commit hash 6ae8c45
# Direct link: https://raw.githubusercontent.com/yannh/kubeconform/6ae8c45bc156ceeb1d421e9b217cfc0c7ba5828d/scripts/openapi2jsonschema.py

import copy
import yaml
import json
import sys
import os
import urllib.request
if 'DISABLE_SSL_CERT_VALIDATION' in os.environ:
    import ssl
    ssl._create_default_https_context = ssl._create_unverified_context

def schema_children(data, *, skip_keywords=()):
    """Yield subschemas, excluding instance data such as defaults and enums."""
    for keyword, value in data.items():
        if keyword in skip_keywords:
            continue
        if keyword in ("properties", "patternProperties", "definitions", "$defs",
                       "dependencies", "dependentSchemas"):
            if isinstance(value, dict):
                for schema in value.values():
                    if isinstance(schema, dict):
                        yield schema
        elif keyword in ("items", "additionalItems", "additionalProperties", "not",
                         "allOf", "anyOf", "oneOf", "contains", "propertyNames",
                         "if", "then", "else", "prefixItems",
                         "unevaluatedItems", "unevaluatedProperties"):
            if isinstance(value, dict):
                yield value
            elif isinstance(value, list):
                for schema in value:
                    if isinstance(schema, dict):
                        yield schema


def additional_properties(data, skip=False):
    "This recreates the behaviour of kubectl at https://github.com/kubernetes/kubernetes/blob/225b9119d6a8f03fcbe3cc3d590c261965d928d0/pkg/kubectl/validation/schema.go#L312"
    if isinstance(data, dict):
        preserve_unknown = data.get("x-kubernetes-preserve-unknown-fields") is True
        if "properties" in data and not skip and not preserve_unknown:
            if "additionalProperties" not in data:
                data["additionalProperties"] = False
        metadata = None
        if data.get("x-kubernetes-embedded-resource") is True:
            # Kubernetes does not apply structural pruning inside embedded metadata.
            metadata = data.get("properties", {}).get("metadata")
        # Combinator branches only constrain fields declared in the structural schema.
        for schema in schema_children(data, skip_keywords=("allOf", "anyOf", "oneOf", "not")):
            if schema is not metadata:
                additional_properties(schema)
    return data


def replace_int_or_string(data):
    data = copy.deepcopy(data)

    def replace(schema):
        legacy_format = schema.get("format") == "int-or-string"
        if legacy_format:
            del schema["format"]
        if legacy_format or schema.get("x-kubernetes-int-or-string") is True:
            schema["type"] = ["string", "integer"]
        for child in schema_children(schema):
            replace(child)

    if isinstance(data, dict):
        replace(data)
    return data


def allow_null_optional_fields(data, parent=None, grand_parent=None, key=None):
    new = {}
    try:
        for k, v in iter(data.items()):
            new_v = v
            if isinstance(v, dict):
                new_v = allow_null_optional_fields(v, data, parent, k)
            elif isinstance(v, list):
                new_v = list()
                for x in v:
                    new_v.append(allow_null_optional_fields(x, v, parent, k))
            elif isinstance(v, str):
                is_non_null_type = k == "type" and v != "null"
                has_required_fields = grand_parent and "required" in grand_parent
                if is_non_null_type and not has_required_fields:
                    new_v = [v, "null"]
            new[k] = new_v
        return new
    except AttributeError:
        return data


def append_no_duplicates(obj, key, value):
    """
    Given a dictionary, lookup the given key, if it doesn't exist create a new array.
    Then check if the given value already exists in the array, if it doesn't add it.
    """
    if key not in obj:
        obj[key] = []
    if value not in obj[key]:
        obj[key].append(value)


def add_nullable_types(schema):
    """Express OpenAPI nullable types as JSON Schema type unions."""
    if not isinstance(schema, dict):
        return
    if schema.get("nullable") is True:
        types = schema.get("type")
        if isinstance(types, str):
            types = [types]
        if isinstance(types, list) and "null" not in types:
            schema["type"] = [*types, "null"]
    for child in schema_children(schema):
        add_nullable_types(child)


def add_set_uniqueness(schema):
    if not isinstance(schema, dict):
        return
    if schema.get("x-kubernetes-list-type") == "set":
        schema["uniqueItems"] = True
    for child in schema_children(schema):
        add_set_uniqueness(child)


def add_embedded_resource_fields(schema):
    if not isinstance(schema, dict):
        return
    if schema.get("x-kubernetes-embedded-resource") is True:
        properties = schema.setdefault("properties", {})
        for name in ("apiVersion", "kind"):
            field = properties.setdefault(name, {})
            field["type"] = "string"
            field["minLength"] = max(field.get("minLength", 0), 1)
            if field.get("default") is None:
                append_no_duplicates(schema, "required", name)
        # Kubernetes decodes null metadata as an empty ObjectMeta.
        properties.setdefault("metadata", {"type": ["object", "null"]})
    for child in schema_children(schema):
        add_embedded_resource_fields(child)


def write_schema_file(schema, filename):
    schemaJSON = ""

    schema = additional_properties(schema, skip=not os.getenv("DENY_ROOT_ADDITIONAL_PROPERTIES"))
    schema = replace_int_or_string(schema)
    add_nullable_types(schema)
    add_set_uniqueness(schema)
    add_embedded_resource_fields(schema)
    schemaJSON = json.dumps(schema, indent=2)

    # Dealing with user input here..
    filename = os.path.basename(filename)
    f = open(filename, "w")
    print(schemaJSON, file=f)
    f.close()
    print("JSON schema written to {filename}".format(filename=filename))


def construct_value(load, node):
    # Handle nodes that start with '='
    # See https://github.com/yaml/pyyaml/issues/89
    if not isinstance(node, yaml.ScalarNode):
        raise yaml.constructor.ConstructorError(
            "while constructing a value",
            node.start_mark,
            "expected a scalar, but found %s" % node.id, node.start_mark
        )
    yield str(node.value)


if __name__ == "__main__":
  if len(sys.argv) < 2:
      print('Missing FILE parameter.\nUsage: %s [FILE]' % sys.argv[0])
      exit(1)

  planned_schemas = []
  for crdFile in sys.argv[1:]:
      if crdFile.startswith("http"):
        f = urllib.request.urlopen(crdFile)
      else:
        f = open(crdFile, "rb")
      with f:
          defs = []
          content = f.read()
          # PyYAML can read JSON numbers in exponent notation as strings.
          try:
              documents = [json.loads(content, parse_constant=str)]
          except json.JSONDecodeError:
              yaml.SafeLoader.add_constructor(u'tag:yaml.org,2002:value', construct_value)
              documents = yaml.load_all(content, Loader=yaml.SafeLoader)
          for y in documents:
              if y is None:
                  continue
              if "items" in y:
                  defs.extend(y["items"])
              if "kind" not in y:
                  continue
              if y["kind"] != "CustomResourceDefinition":
                  continue
              else:
                  defs.append(y)

          for y in defs:
              filename_format = os.getenv("FILENAME_FORMAT", "{kind}_{version}")
              filename = ""
              if "spec" in y and "versions" in y["spec"] and y["spec"]["versions"]:
                  for version in y["spec"]["versions"]:
                      if "schema" in version and "openAPIV3Schema" in version["schema"]:
                          filename = filename_format.format(
                              kind=y["spec"]["names"]["kind"],
                              group=y["spec"]["group"].split(".")[0],
                              fullgroup=y["spec"]["group"],
                              version=version["name"],
                          ).lower() + ".json"

                          schema = version["schema"]["openAPIV3Schema"]
                          planned_schemas.append((schema, filename, crdFile))
                      elif "validation" in y["spec"] and "openAPIV3Schema" in y["spec"]["validation"]:
                          filename = filename_format.format(
                              kind=y["spec"]["names"]["kind"],
                              group=y["spec"]["group"].split(".")[0],
                              fullgroup=y["spec"]["group"],
                              version=version["name"],
                          ).lower() + ".json"

                          schema = y["spec"]["validation"]["openAPIV3Schema"]
                          planned_schemas.append((schema, filename, crdFile))
              elif "spec" in y and "validation" in y["spec"] and "openAPIV3Schema" in y["spec"]["validation"]:
                  filename = filename_format.format(
                      kind=y["spec"]["names"]["kind"],
                      group=y["spec"]["group"].split(".")[0],
                      fullgroup=y["spec"]["group"],
                      version=y["spec"]["version"],
                  ).lower() + ".json"

                  schema = y["spec"]["validation"]["openAPIV3Schema"]
                  planned_schemas.append((schema, filename, crdFile))

  # Discover every output before publishing any schema. Different groups can
  # share kind/version names, and custom formats are flattened to basenames.
  local_inputs = {}
  for source in sys.argv[1:]:
      if not source.startswith("http"):
          info = os.stat(source)
          local_inputs[(info.st_dev, info.st_ino)] = source

  sources_by_filename = {}
  for schema, filename, source in planned_schemas:
      output = os.path.basename(filename)
      if output in sources_by_filename:
          sys.exit(f"Duplicate output filename {output!r}: "
                   f"{sources_by_filename[output]} and {source}; "
                   "use a distinct FILENAME_FORMAT such as "
                   "{fullgroup}_{kind}_{version} or separate the inputs")
      sources_by_filename[output] = source

      try:
          info = os.stat(output)
      except FileNotFoundError:
          continue
      input_path = local_inputs.get((info.st_dev, info.st_ino))
      if input_path is not None:
          sys.exit(f"Output filename {output!r} would overwrite input file {input_path!r}; "
                   "use a distinct FILENAME_FORMAT or run from another directory")

  for schema, filename, source in planned_schemas:
      write_schema_file(schema, filename)

  exit(0)
