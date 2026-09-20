"""Bounded JSON Schema allOf intersection, checked against an independent oracle."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from payload_palette import (
    AsyncGenerationRunner,
    GeneratedResponse,
    GenerationPolicy,
    GenerationRequest,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    RuleBinding,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    StringChoices,
    TokenUsage,
    ValidationPipeline,
    export_output_config,
    export_output_schema,
    load_output_config,
    load_output_schema,
)


def test_imports_distinct_typed_all_of() -> None:
    schema = load_output_schema(
        {"allOf": [{"type": "integer", "minimum": 2}, {"type": "integer", "maximum": 5}]}
    )
    assert schema.kind == "all_of"
    assert len(schema.all_of) == 2


@pytest.mark.parametrize(
    "document,candidates",
    [
        (
            {"allOf": [{"type": "integer", "minimum": 2}, {"type": "integer", "maximum": 5}]},
            (1, 2, 3.0, 5, 6, True, "3"),
        ),
        (
            {
                "allOf": [
                    {"type": "string", "minLength": 2},
                    {"type": "string", "enum": ["ab", "xyz"]},
                ]
            },
            ("", "a", "ab", "xyz", "abc", 1),
        ),
        (
            {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}},
                        "required": ["a"],
                        "additionalProperties": True,
                    },
                    {
                        "type": "object",
                        "properties": {"b": {"type": "string"}},
                        "required": ["b"],
                        "additionalProperties": True,
                    },
                ]
            },
            ({"a": 2, "b": "x"}, {"a": 2}, {"b": "x"}, {"a": "2", "b": "x"}),
        ),
        (
            {
                "allOf": [
                    {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}},
                        "required": ["a"],
                        "additionalProperties": False,
                    },
                    {
                        "type": "object",
                        "properties": {"b": {"type": "integer"}},
                        "required": ["b"],
                        "additionalProperties": False,
                    },
                ]
            },
            ({"a": 1, "b": 2}, {"a": 1}, {"b": 2}, {}),
        ),
        (
            {
                "allOf": [
                    {"type": "array", "items": {"type": "integer"}, "minItems": 2},
                    {"type": "array", "items": {"type": "number"}, "maxItems": 3},
                ]
            },
            ([], [1], [1, 2], [1, 2.0, 3], [1, 2, 3, 4], [1, "x"]),
        ),
        (
            {
                "allOf": [
                    {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                    {
                        "oneOf": [
                            {"type": "integer", "minimum": 0},
                            {"type": "string", "minLength": 2},
                        ]
                    },
                ]
            },
            (-1, 0, 2, "a", "ab", None),
        ),
        (
            {
                "$defs": {
                    "lower": {"type": "number", "minimum": 0},
                    "upper": {"type": "number", "maximum": 10},
                },
                "allOf": [{"$ref": "#/$defs/lower"}, {"$ref": "#/$defs/upper"}],
            },
            (-1, 0, 2.5, 10, 11, "2"),
        ),
    ],
)
def test_all_of_matches_independent_draft202012_oracle(
    document: dict[str, Any], candidates: tuple[Any, ...]
) -> None:
    Draft202012Validator.check_schema(document)
    oracle = Draft202012Validator(document)
    schema = load_output_schema(document)
    exported = export_output_schema(schema)
    reloaded = load_output_schema(exported)
    assert exported["allOf"] and reloaded.kind == "all_of"
    for candidate in candidates:
        original = deepcopy(candidate)
        expected = oracle.is_valid(candidate)
        assert (not schema.validate(candidate)) is expected
        assert (not reloaded.validate(candidate)) is expected
        assert candidate == original


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "all_of"},
        {"kind": "all_of", "all_of": (OutputSchema("null"),)},
        {"kind": "all_of", "all_of": (OutputSchema("null"),) * 17},
        {"kind": "all_of", "all_of": [OutputSchema("null"), OutputSchema("string")]},
        {"kind": "all_of", "all_of": (OutputSchema("null"), object())},
        {"kind": "all_of", "any_of": (OutputSchema("null"), OutputSchema("string"))},
        {"kind": "one_of", "all_of": (OutputSchema("null"), OutputSchema("string"))},
        {
            "kind": "all_of",
            "all_of": (OutputSchema("null"), OutputSchema("string")),
            "enum": (None,),
        },
    ],
)
def test_all_of_constructor_rejects_malformed_and_cross_kind_branches(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        OutputSchema(**kwargs)


@pytest.mark.parametrize(
    "document",
    [
        {"allOf": []},
        {"allOf": [{"type": "null"}]},
        {"allOf": [{"type": "null"}] * 17},
        {"allOf": "not a list"},
        {"allOf": [{"type": "null"}, True]},
        {"allOf": [{"type": "number"}, {"minimum": 1}]},
        {"allOf": [{"type": "null"}, {"type": "string"}], "type": "string"},
        {"allOf": [{"type": "null"}, {"type": "string"}], "anyOf": []},
        {"allOf": [{"type": "null"}, {"type": "string"}], "oneOf": []},
        {"allOf": [{"type": "null"}, {"type": "string"}], "description": "ignored?"},
        {"$defs": {"bad": {"allOf": [{"type": "null"}, {"$ref": "#/$defs/bad"}]}}, "type": "null"},
        {"$defs": {"bad": {"allOf": [{"type": "null"}, {"type": "bogus"}]}}, "type": "null"},
        {"allOf": [{"$ref": "#/$defs/missing"}, {"type": "null"}]},
    ],
)
def test_all_of_import_rejects_unsupported_or_malformed(document: Any) -> None:
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(document)


def test_all_of_definition_and_expanded_budgets() -> None:
    document = {
        "$defs": {"num": {"type": "integer", "x-payload-strict-integer": True}},
        "allOf": [{"$ref": "#/$defs/num"}, {"type": "number", "minimum": 1}],
    }
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(document, limits=SchemaDefinitionLimits(max_nodes=2))
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(document, limits=SchemaDefinitionLimits(max_depth=0))
    with pytest.raises(SchemaDefinitionError, match="branches"):
        load_output_schema(
            {"allOf": [{"type": "null"}] * 3},
            limits=SchemaDefinitionLimits(max_branches=2),
        )
    schema = load_output_schema(document)
    assert schema.validate(2) == ()
    assert [issue.code for issue in schema.validate(2.0)] == ["schema_all_of"]
    leaf = OutputSchema("string")
    broad = OutputSchema("all_of", all_of=(leaf,) * 16)
    broader = OutputSchema("all_of", all_of=(broad,) * 16)
    with pytest.raises(ValueError, match="4096-node"):
        OutputSchema("all_of", all_of=(broader,) * 16)


def test_all_of_consumes_shared_work_for_every_branch() -> None:
    schema = OutputSchema("all_of", all_of=(OutputSchema("integer"), OutputSchema("number")))
    with pytest.raises(OutputContractError, match="work"):
        schema.validate(2, limits=OutputLimits(max_schema_steps=2))
    assert schema.validate(2, limits=OutputLimits(max_schema_steps=3)) == ()
    with pytest.raises(OutputContractError, match="work"):
        schema.validate("private", limits=OutputLimits(max_schema_steps=2))
    assert [
        issue.code for issue in schema.validate("private", limits=OutputLimits(max_schema_steps=3))
    ] == ["schema_all_of"]
    nested = OutputSchema("all_of", all_of=(schema,) * 16)
    with pytest.raises(OutputContractError, match="work"):
        nested.validate(2, limits=OutputLimits(max_schema_steps=40))


def test_all_of_collapses_private_branch_failures_at_candidate_path() -> None:
    schema = OutputSchema(
        "object",
        properties={
            "choice": OutputSchema(
                "all_of",
                all_of=(
                    OutputSchema("object", properties={"known": OutputSchema("string")}),
                    OutputSchema("object", properties={"known": OutputSchema("integer")}),
                ),
            )
        },
    )
    issues = schema.validate({"choice": {"secret-key": "secret-value"}})
    assert [(issue.code, issue.path) for issue in issues] == [("schema_all_of", '$["choice"]')]
    assert "secret" not in str(issues)
    with_sibling = OutputSchema(
        "object",
        properties={"required": OutputSchema("null"), "choice": schema.properties["choice"]},
        required=("required",),
    )
    capped = with_sibling.validate(
        {"choice": {"secret-key": "secret-value"}}, limits=OutputLimits(max_issues=1)
    )
    assert [issue.code for issue in capped] == ["schema_required", "schema_issue_limit"]


def test_all_of_config_path_traversal_and_generation() -> None:
    compatible = OutputSchema(
        "all_of",
        all_of=(
            OutputSchema(
                "object", properties={"label": OutputSchema("string")}, additional_properties=True
            ),
            OutputSchema(
                "object", properties={"label": OutputSchema("string")}, additional_properties=False
            ),
        ),
    )
    pipeline = ValidationPipeline(
        compatible, (RuleBinding("label", ("label",), StringChoices(("ok",))),)
    )
    restored = load_output_config(export_output_config(pipeline))
    assert restored.schema.kind == "all_of"
    assert restored.validate({"label": "ok"}).valid
    assert not restored.validate({"label": "bad"}).valid
    impossible = OutputSchema(
        "all_of",
        all_of=(
            OutputSchema("object", properties={"label": OutputSchema("string")}),
            OutputSchema("object"),
        ),
    )
    with pytest.raises(ValueError, match="incompatible"):
        ValidationPipeline(impossible, (RuleBinding("label", ("label",), StringChoices(("ok",))),))

    class Provider:
        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            return GeneratedResponse('{"label":"ok"}', TokenUsage(0, 0))

    report = asyncio.run(
        AsyncGenerationRunner(restored, Provider(), GenerationPolicy(max_attempts=1)).run("go")
    )
    assert report.valid and report.output == {"label": "ok"}
