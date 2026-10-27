"""JSON Schema evaluation without implicit network or filesystem retrieval."""
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry
from referencing.exceptions import NoSuchResource
from referencing.jsonschema import DRAFT202012


def _deny_retrieval(uri):
    raise NoSuchResource(ref=uri)


OFFLINE_REGISTRY = Registry(retrieve=_deny_retrieval)


def check_local_schema(schema):
    """Check real schema nodes; instance examples and annotations stay opaque."""
    # Validate against the bundled meta-schema with the same fail-closed
    # registry used for actual input validation, including its built-in resources.
    meta = Draft202012Validator(Draft202012Validator.META_SCHEMA,
        registry=OFFLINE_REGISTRY, format_checker=Draft202012Validator.FORMAT_CHECKER)
    for error in meta.iter_errors(schema):
        raise SchemaError.create_from(error)
    pending = [DRAFT202012.create_resource(schema)]
    while pending:
        resource = pending.pop()
        node = resource.contents
        if isinstance(node, dict):
            for keyword in ("$ref", "$dynamicRef"):
                if keyword in node and (not isinstance(node[keyword], str) or not node[keyword].startswith("#")):
                    raise SchemaError("JSON Schema 仅允许本地片段引用")
        # The specification knows which keyword positions are sub-schemas.
        # Recursing arbitrary dictionaries would misinterpret literal $ref keys
        # in examples/default/const/enum or unknown annotation payloads.
        pending.extend(resource.subresources())


def offline_validator(schema):
    check_local_schema(schema)
    return Draft202012Validator(schema, registry=OFFLINE_REGISTRY)
