from __future__ import annotations

import asyncio
from typing import Any

import pytest

from payload_palette import (
    AsyncGenerationRunner,
    GeneratedResponse,
    GenerationPolicy,
    GenerationRequest,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    PayloadValidationError,
    RuleBinding,
    RuleContext,
    RuleResult,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    StringChoices,
    TokenUsage,
    ValidationPipeline,
    export_output_schema,
    load_output_schema,
    load_output_schema_json_bytes,
)


@pytest.mark.parametrize(
    ("document", "valid_values", "invalid_values"),
    [
        ({"type": "string", "minLength": 1, "maxLength": 3}, ["a", "abc", "😀"], ["", "abcd", 1]),
        ({"type": "number", "minimum": 0, "maximum": 1}, [0, 0.5, 1], [-1, 2, True, "1"]),
        ({"type": "integer", "enum": [1, 2]}, [1, 1.0, 2], [True, 1.5, 3]),
        ({"type": "boolean", "const": True}, [True], [False, 1]),
        ({"type": "null"}, [None], [False, 0]),
        ({"type": ["string", "null"]}, [None, "yes"], [1, False, []]),
        (
            {"anyOf": [{"type": "integer"}, {"type": "string", "minLength": 2}]},
            [1, "ok"],
            [True, "x", None],
        ),
        (
            {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
            [["a"], ["a", "b"]],
            [[], [1], ["a", "b", "c"]],
        ),
        ({"type": "object"}, [{}, {"unlisted": [None, True]}], [None, []]),
        ({"type": "object", "additionalProperties": False}, [{}], [{"extra": 1}]),
        (
            {"type": "object", "additionalProperties": {"type": "integer"}},
            [{}, {"x": 1}],
            [{"x": True}, {"x": "1"}],
        ),
        (
            {
                "type": "object",
                "properties": {"n": {"type": "integer"}},
                "required": ["n"],
                "additionalProperties": False,
            },
            [{"n": 1}],
            [{}, {"n": "1"}, {"n": 1, "extra": 2}],
        ),
    ],
)
def test_independent_supported_keyword_conformance(
    document: Any, valid_values: list[Any], invalid_values: list[Any]
) -> None:
    schema = load_output_schema(document)
    for value in valid_values:
        assert schema.validate(value) == ()
    for value in invalid_values:
        assert schema.validate(value)


def test_schema_roundtrip_preserves_typed_maps_unions_and_immutable_snapshots() -> None:
    schema = OutputSchema(
        "object",
        properties={"fixed": OutputSchema("string", enum=("known",))},
        required=("fixed",),
        additional_properties=OutputSchema(
            "array", items=OutputSchema("integer", minimum=0).nullable()
        ),
    )
    exported = export_output_schema(schema)
    loaded = load_output_schema(exported)
    assert loaded.json_schema() == schema.json_schema()
    assert loaded.validate({"fixed": "known", "dynamic": [0, None, 2]}) == ()
    assert loaded.validate({"fixed": "known", "dynamic": [-1]})
    exported.clear()
    assert loaded.validate({"fixed": "known"}) == ()
    assert export_output_schema(schema, include_dialect=False) == schema.json_schema(
        preserve_python_types=True
    )
    assert load_output_schema({"type": ["string"]}).kind == "string"


@pytest.mark.parametrize(
    "document",
    [
        True,
        [],
        {},
        {"type": "unknown"},
        {"type": "__import__('os')"},
        {"type": "string", "$ref": "file:///private"},
        {"type": "string", "unknown": 1},
        {"type": "string", "minimum": 0},
        {"type": "number", "minimum": None},
        {"type": "number", "minimum": True},
        {"type": "number", "minimum": float("nan")},
        {"type": "number", "minimum": 10**1000},
        {"type": "number", "minimum": 2, "maximum": 1},
        {"type": "string", "minLength": None},
        {"type": "string", "maxLength": True},
        {"type": "string", "minLength": -1},
        {"type": "string", "enum": None},
        {"type": "string", "enum": []},
        {"type": "string", "enum": ["a"], "const": "a"},
        {"type": "string", "enum": [{}]},
        {"type": "array"},
        {"type": "array", "items": None},
        {"type": "array", "items": {"type": "null"}, "maxItems": None},
        {"type": "object", "properties": None},
        {"type": "object", "properties": {"\ud800": {"type": "null"}}},
        {"type": "object", "required": "a"},
        {"type": "object", "required": [1]},
        {"type": "object", "required": ["missing"]},
        {"type": "object", "additionalProperties": None},
        {"anyOf": []},
        {"anyOf": [{"type": "null"}]},
        {"anyOf": [{"type": "null"}] * 17},
        {"anyOf": [{"type": "null"}, {"type": "string"}], "type": "object"},
        {"type": ["string", "string"]},
        {"type": []},
        {"type": [1]},
        {"type": ["string", "null"], "minLength": 1},
        {"type": "string", "$schema": "https://example.invalid/dialect"},
        {
            "type": "array",
            "items": {"type": "string", "$schema": "https://json-schema.org/draft/2020-12/schema"},
        },
        {**{str(index): None for index in range(9)}, "type": "string"},
    ],
)
def test_import_rejects_unknown_or_invalid_definitions(document: Any) -> None:
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(document)


def test_non_string_keyword_keys_never_invoke_comparison_hooks() -> None:
    class Key:
        def __hash__(self) -> int:
            return hash("type")

        def __eq__(self, other: object) -> bool:
            raise AssertionError("untrusted equality invoked")

    with pytest.raises(SchemaDefinitionError):
        load_output_schema({Key(): "string"})


def test_strict_schema_json_decoder_rejects_duplicate_keys_and_oversized_bytes() -> None:
    assert load_output_schema_json_bytes(b'{"type":"integer"}').kind == "integer"
    with pytest.raises(PayloadValidationError, match="duplicate_json_key"):
        load_output_schema_json_bytes(b'{"type":"integer","type":"string"}')
    with pytest.raises(PayloadValidationError, match="input_too_large"):
        load_output_schema_json_bytes(b'{"type":"integer"}', max_input_bytes=2)


def test_import_cycles_shared_children_and_definition_budgets() -> None:
    cyclic: dict[str, Any] = {"type": "object", "properties": {}}
    cyclic["properties"]["child"] = cyclic
    with pytest.raises(SchemaDefinitionError, match="schema_definition_cycle"):
        load_output_schema(cyclic)
    leaf = {"type": "string"}
    shared = {"type": "object", "properties": {"a": leaf, "b": leaf}}
    assert not load_output_schema(shared).validate({"a": "x", "b": "y"})
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(shared, limits=SchemaDefinitionLimits(max_nodes=2))
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(shared, limits=SchemaDefinitionLimits(max_depth=0))
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(
            {"type": "string", "enum": ["long"]}, limits=SchemaDefinitionLimits(max_characters=2)
        )
    with pytest.raises(SchemaDefinitionError, match="branches"):
        load_output_schema(
            {"anyOf": [leaf, {"type": "null"}, {"type": "integer"}]},
            limits=SchemaDefinitionLimits(max_branches=2),
        )


def test_union_work_budget_never_resets_between_candidates() -> None:
    schema = OutputSchema(
        "union",
        any_of=(
            OutputSchema("array", items=OutputSchema("integer")),
            OutputSchema("array", items=OutputSchema("string")),
        ),
    )
    assert schema.validate(["a"], limits=OutputLimits(max_schema_steps=5)) == ()
    with pytest.raises(OutputContractError, match="work"):
        schema.validate(["a"], limits=OutputLimits(max_schema_steps=4))
    assert schema.validate([True])[0].code == "schema_any_of"


def test_union_branch_errors_do_not_leak_or_hide_sibling_failures() -> None:
    choice = OutputSchema("integer").nullable()
    schema = OutputSchema(
        "object",
        properties={"required": OutputSchema("null"), "choice": choice},
        required=("required",),
    )
    issues = schema.validate({"choice": "bad"})
    assert [issue.code for issue in issues] == ["schema_required", "schema_any_of"]
    assert [
        issue.code
        for issue in schema.validate({"choice": "bad"}, limits=OutputLimits(max_issues=1))
    ] == ["schema_required", "schema_issue_limit"]
    assert choice.nullable() is choice
    null = OutputSchema("null")
    assert null.nullable() is null


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "union"},
        {"kind": "union", "any_of": []},
        {"kind": "union", "any_of": (object(), OutputSchema("null"))},
        {"kind": "string", "any_of": (OutputSchema("null"), OutputSchema("string"))},
        {
            "kind": "union",
            "any_of": (OutputSchema("null"), OutputSchema("string")),
            "enum": (None,),
        },
        {"kind": "integer", "additional_properties": OutputSchema("null")},
    ],
)
def test_invalid_extended_model_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OutputSchema(**kwargs)


def test_union_paths_and_typed_dynamic_maps_keep_pipeline_validation_sound() -> None:
    schema = OutputSchema(
        "object", properties={"age": OutputSchema("integer")}, required=("age",)
    ).nullable()

    class Adult:
        def check(self, value: Any, context: RuleContext) -> RuleResult:
            return (
                RuleResult(True)
                if value >= 18
                else RuleResult(False, "under_age", "requires adult")
            )

    pipeline = ValidationPipeline(schema, (RuleBinding("adult", ("age",), Adult()),))
    assert pipeline.validate(None).valid
    assert not pipeline.validate({"age": 12}).valid
    assert pipeline.validate({"age": 20}).valid
    with pytest.raises(ValueError, match="incompatible"):
        ValidationPipeline(schema, (RuleBinding("typo", ("ag",), Adult()),))
    mapping = OutputSchema("object", additional_properties=OutputSchema("integer"))
    assert (
        not ValidationPipeline(mapping, (RuleBinding("adult", ("person",), Adult()),))
        .validate({"person": 12})
        .valid
    )
    with pytest.raises(ValueError, match="incompatible"):
        ValidationPipeline(mapping, (RuleBinding("bad", ("person", "age"), Adult()),))


def test_union_generation_feedback_preserves_declared_fields_and_redacts_dynamic_keys() -> None:
    class Provider:
        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            return GeneratedResponse('{"label":"bad","private-key":"bad"}', TokenUsage(0, 0))

    schema = OutputSchema(
        "object",
        properties={"label": OutputSchema("string")},
        additional_properties=OutputSchema("string"),
    ).nullable()
    pipeline = ValidationPipeline(
        schema,
        (
            RuleBinding("declared", ("label",), StringChoices(("ok",))),
            RuleBinding("dynamic", ("private-key",), StringChoices(("ok",))),
        ),
    )
    report = asyncio.run(
        AsyncGenerationRunner(pipeline, Provider(), GenerationPolicy(max_attempts=1)).run(
            "classify"
        )
    )
    assert [feedback.path for feedback in report.attempts[0].feedback] == ['$["label"]', "$"]
    assert "private-key" not in str(report.to_dict())


@pytest.mark.parametrize("name", ["max_depth", "max_nodes", "max_characters", "max_branches"])
@pytest.mark.parametrize("value", [True, -1, 1.0, 10**100])
def test_definition_limits_reject_invalid_configuration(name: str, value: Any) -> None:
    with pytest.raises(ValueError):
        SchemaDefinitionLimits(**{name: value})


def test_export_and_import_configuration_types_are_strict() -> None:
    with pytest.raises(ValueError):
        load_output_schema({"type": "string"}, limits={})  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        export_output_schema(None)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        export_output_schema(OutputSchema("null"), include_dialect=1)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OutputLimits(max_schema_steps=True)


@pytest.mark.parametrize(
    "document",
    [
        {"type": "integer", "enum": [1, 1.0]},
        {"type": "string", "enum": ["a", "a"]},
        {"type": "integer", "x-payload-strict-integer": False},
        {"type": "integer", "x-payload-strict-integer": 1},
        {"type": "number", "x-payload-strict-integer": True},
    ],
)
def test_interchange_extensions_and_enum_definitions_are_strict(document):
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(document)


def test_untrusted_dialect_value_never_invokes_comparison():
    class Unexpected:
        def __eq__(self, other):
            raise AssertionError("comparison invoked")

    with pytest.raises(SchemaDefinitionError):
        load_output_schema({"type": "string", "$schema": Unexpected()})


def test_union_definition_constructor_failures_remain_structured(monkeypatch):
    import payload_palette.schema_io as schema_io

    original = schema_io.OutputSchema

    def reject_union(kind, **kwargs):
        if kind == "union":
            raise ValueError("resource limit")
        return original(kind, **kwargs)

    monkeypatch.setattr(schema_io, "OutputSchema", reject_union)
    with pytest.raises(SchemaDefinitionError, match="schema_definition_constraint"):
        load_output_schema({"anyOf": [{"type": "null"}, {"type": "string"}]})


def test_union_fix_cannot_bypass_final_schema_validation():
    class BadRepair:
        def check(self, value, context):
            if value == "old":
                return RuleResult(False, "repair", "replace", 42)
            return RuleResult(True)

    schema = OutputSchema("string").nullable()
    report = ValidationPipeline(schema, (RuleBinding("repair", (), BadRepair(), "fix"),)).validate(
        "old"
    )
    assert not report.valid and report.output is None
    assert any(issue.code == "schema_any_of" for issue in report.issues)


@pytest.mark.parametrize("mode", [None, True, "wrong"])
def test_integer_mode_configuration_is_strict(mode):
    with pytest.raises(ValueError):
        OutputSchema("integer", integer_mode=mode)


def test_integer_mode_only_applies_to_integer_and_export_flag_is_strict():
    with pytest.raises(ValueError):
        OutputSchema("string", integer_mode="json")
    with pytest.raises(ValueError):
        OutputSchema("null").json_schema(preserve_python_types=1)


@pytest.mark.parametrize("enum", [[10**1000], [float("inf")], ["a" * 2049], ["\ud800"]])
def test_schema_enum_rejects_oversized_or_nonfinite_values_before_comparison(enum):
    with pytest.raises(SchemaDefinitionError):
        load_output_schema({"type": "string" if type(enum[0]) is str else "number", "enum": enum})


def test_export_normalizes_repeated_constructor_enums_without_changing_semantics():
    schema = OutputSchema("number", enum=(1, 1.0, 2))
    exported = export_output_schema(schema)
    assert exported["enum"] == [1, 2]
    loaded = load_output_schema(exported)
    for value in (1, 1.0, 2, 2.0, 3, True):
        assert bool(loaded.validate(value)) == bool(schema.validate(value))
