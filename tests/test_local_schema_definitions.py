"""Bounded local JSON Schema definition/reference interoperability."""

from __future__ import annotations

from copy import deepcopy

import pytest

from payload_palette import (
    RuleBinding,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    StringChoices,
    ValidationPipeline,
    export_output_schema,
    load_output_schema,
    load_output_schema_json_bytes,
)


def _address_document() -> dict[str, object]:
    return {
        "$defs": {
            "address": {
                "type": "object",
                "properties": {
                    "city": {"$ref": "#/$defs/city"},
                    "zip": {"type": "integer", "minimum": 0},
                },
                "required": ["city", "zip"],
                "additionalProperties": False,
            },
            "city": {"type": "string", "minLength": 1},
        },
        "type": "object",
        "properties": {
            "home": {"$ref": "#/$defs/address"},
            "work": {"$ref": "#/$defs/address"},
        },
        "required": ["home", "work"],
        "additionalProperties": False,
    }


def test_local_definitions_reuse_nested_aliases_and_export_without_leaking_input() -> None:
    document = _address_document()
    original = deepcopy(document)
    schema = load_output_schema(document)
    assert document == original
    accepted = {"home": {"city": "A", "zip": 1}, "work": {"city": "B", "zip": 2}}
    assert schema.validate(accepted) == ()
    assert schema.validate({**accepted, "work": {"city": "", "zip": 2}})
    with_dialect = load_output_schema(
        {"$schema": "https://json-schema.org/draft/2020-12/schema", **document}
    )
    assert with_dialect.validate(accepted) == ()
    exported = export_output_schema(schema)
    assert "$defs" not in exported
    assert load_output_schema(exported).validate(accepted) == ()
    document["$defs"] = {}
    assert schema.validate(accepted) == ()


def test_json_pointer_escaped_definition_name_and_json_bytes() -> None:
    schema = load_output_schema_json_bytes(
        b'{"$defs":{"a/b~c":{"type":"string","const":"ok"}},'
        b'"type":"array","items":{"$ref":"#/$defs/a~1b~0c"}}'
    )
    assert schema.validate(["ok", "ok"]) == ()
    assert schema.validate(["ok", "no"])


def test_maximum_length_definition_name_remains_addressable_when_fully_escaped() -> None:
    name = "/" * 256
    schema = load_output_schema(
        {"$defs": {name: {"type": "string"}}, "$ref": "#/$defs/" + "~1" * 256}
    )
    assert schema.validate("ok") == ()
    assert schema.validate(1)


def test_referenced_fields_remain_declared_to_semantic_rule_bindings() -> None:
    pipeline = ValidationPipeline(
        load_output_schema(_address_document()),
        (RuleBinding("home_city", ("home", "city"), StringChoices(("A",))),),
    )
    accepted = {"home": {"city": "A", "zip": 1}, "work": {"city": "B", "zip": 2}}
    assert pipeline.validate(accepted).valid
    assert not pipeline.validate({**accepted, "home": {"city": "C", "zip": 1}}).valid


@pytest.mark.parametrize(
    "document",
    [
        {"type": "string", "$defs": {"bad": {"type": "wat"}}},
        {"type": "string", "$defs": {"bad": {"$ref": "#/$defs/missing"}}},
        {"$ref": "#/$defs/missing", "$defs": {"known": {"type": "string"}}},
        {"$ref": "#/$defs/x", "$defs": {"x": {"$ref": "#/$defs/y"}, "y": {"$ref": "#/$defs/x"}}},
        {
            "$defs": {"node": {"type": "object", "properties": {"next": {"$ref": "#/$defs/node"}}}},
            "$ref": "#/$defs/node",
        },
        {"$ref": "#/$defs/x", "$defs": {"x": {"type": "string"}}, "type": "string"},
        {"$ref": "https://example.invalid/schema"},
        {"$ref": 3},
        {"$ref": "#/$defs/", "$defs": {}},
        {"$ref": "#/$defs/a~2b", "$defs": {"a~2b": {"type": "null"}}},
        {"$ref": "#/$defs/a~", "$defs": {"a~": {"type": "null"}}},
        {"$ref": "#/$defs/a/b", "$defs": {"a": {"type": "object"}}},
        {"$ref": "#/$defs/a%2Fb", "$defs": {"a/b": {"type": "string"}}},
        {"type": "string", "$defs": []},
        {"type": "array", "items": {"$defs": {"x": {"type": "null"}}, "$ref": "#/$defs/x"}},
        {"type": "string", "$defs": {"\ud800": {"type": "string"}}},
        {"type": "string", "$defs": {"a%b": {"type": "string"}}},
        {"type": "string", "$defs": {"x" * 257: {"type": "string"}}},
    ],
)
def test_invalid_references_and_even_unused_definitions_are_rejected(
    document: dict[str, object],
) -> None:
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(document)


def test_reference_expansion_is_charged_to_existing_node_and_depth_budgets() -> None:
    document = _address_document()
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(document, limits=SchemaDefinitionLimits(max_nodes=8))
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(document, limits=SchemaDefinitionLimits(max_depth=2))
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(
            {"$defs": {"x": {"type": "string"}}, "$ref": "#/$defs/x"},
            limits=SchemaDefinitionLimits(max_characters=1),
        )
