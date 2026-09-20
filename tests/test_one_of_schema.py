"""Exact-one schema semantics and boundaries, separate from anyOf unions."""

from __future__ import annotations

import asyncio
import json
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
    RuleBinding,
    RuleContext,
    RuleResult,
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
from payload_palette.cli import run


def test_one_of_later_overlap_must_consume_shared_schema_work() -> None:
    schema = OutputSchema(
        "one_of",
        one_of=(
            OutputSchema("object", properties={"x": OutputSchema("integer")}),
            OutputSchema("object", properties={"x": OutputSchema("number")}),
        ),
    )
    with pytest.raises(OutputContractError, match="work"):
        schema.validate({"x": 1}, limits=OutputLimits(max_schema_steps=4))
    assert [
        issue.code for issue in schema.validate({"x": 1}, limits=OutputLimits(max_schema_steps=5))
    ] == ["schema_one_of"]
    assert schema.validate({"x": "bad"}, limits=OutputLimits(max_schema_steps=5)) == (
        schema.validate({"x": "bad"})[0],
    )


def test_one_of_collapses_private_branch_failures_at_candidate_path() -> None:
    schema = OutputSchema(
        "object",
        properties={
            "choice": OutputSchema(
                "one_of",
                one_of=(
                    OutputSchema("object", properties={"known": OutputSchema("string")}),
                    OutputSchema("object", properties={"known": OutputSchema("integer")}),
                ),
            )
        },
    )
    issues = schema.validate({"choice": {"secret-key": "secret-value"}})
    assert [(issue.code, issue.path) for issue in issues] == [("schema_one_of", '$["choice"]')]
    assert "secret" not in str(issues)
    limited = schema.validate(
        {"choice": {"secret-key": "secret-value"}}, limits=OutputLimits(max_issues=1)
    )
    assert [issue.code for issue in limited] == ["schema_one_of"]
    with_sibling = OutputSchema(
        "object",
        properties={"required": OutputSchema("null"), "choice": schema.properties["choice"]},
        required=("required",),
    )
    capped = with_sibling.validate(
        {"choice": {"secret-key": "secret-value"}}, limits=OutputLimits(max_issues=1)
    )
    assert [issue.code for issue in capped] == ["schema_required", "schema_issue_limit"]


def test_one_of_nullable_does_not_preserve_overlapping_null_rejection() -> None:
    schema = OutputSchema(
        "one_of",
        one_of=(
            OutputSchema("null"),
            OutputSchema("union", any_of=(OutputSchema("null"), OutputSchema("string"))),
        ),
    )
    assert [issue.code for issue in schema.validate(None)] == ["schema_one_of"]
    nullable = schema.nullable()
    assert nullable is not schema
    assert nullable.validate(None) == ()
    assert nullable.validate("text") == ()
    assert [issue.code for issue in nullable.validate(1)] == ["schema_any_of"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "one_of"},
        {"kind": "one_of", "one_of": (OutputSchema("null"),)},
        {"kind": "one_of", "one_of": (OutputSchema("null"),) * 17},
        {"kind": "one_of", "one_of": [OutputSchema("null"), OutputSchema("string")]},
        {"kind": "one_of", "one_of": (OutputSchema("null"), object())},
        {"kind": "one_of", "any_of": (OutputSchema("null"), OutputSchema("string"))},
        {"kind": "union", "one_of": (OutputSchema("null"), OutputSchema("string"))},
        {
            "kind": "one_of",
            "one_of": (OutputSchema("null"), OutputSchema("string")),
            "enum": (None,),
        },
    ],
)
def test_one_of_constructor_rejects_malformed_and_cross_kind_branches(
    kwargs: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        OutputSchema(**kwargs)


@pytest.mark.parametrize(
    "document",
    [
        {"oneOf": []},
        {"oneOf": [{"type": "null"}]},
        {"oneOf": [{"type": "null"}] * 17},
        {"oneOf": [{"type": "null"}, {"type": "string"}], "type": "string"},
        {"oneOf": [{"type": "null"}, {"type": "string"}], "anyOf": []},
        {
            "type": "array",
            "items": {"oneOf": [{"type": "null"}, {"type": "string"}], "enum": [None]},
        },
        {"$defs": {"bad": {"oneOf": [{"type": "null"}, {"$ref": "#/$defs/bad"}]}}, "type": "null"},
        {"$defs": {"bad": {"oneOf": [{"type": "null"}, {"type": "bogus"}]}}, "type": "null"},
    ],
)
def test_one_of_import_rejects_unsupported_or_malicious_definitions(document: Any) -> None:
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(document)


def test_one_of_expansion_charges_definition_limits_and_preserves_strict_integer() -> None:
    document = {
        "$defs": {"num": {"type": "integer", "x-payload-strict-integer": True}},
        "oneOf": [{"$ref": "#/$defs/num"}, {"type": "string"}],
    }
    with pytest.raises(SchemaDefinitionError, match="budget"):
        load_output_schema(document, limits=SchemaDefinitionLimits(max_nodes=2))
    with pytest.raises(SchemaDefinitionError, match="branches"):
        load_output_schema(
            {"oneOf": [{"type": "null"}] * 3},
            limits=SchemaDefinitionLimits(max_branches=2),
        )
    schema = load_output_schema(document)
    exported = export_output_schema(schema)
    assert exported["oneOf"][0] == {"type": "integer", "x-payload-strict-integer": True}
    reloaded = load_output_schema(exported)
    assert reloaded.kind == "one_of"
    for value in (1, 1.0, "x", None):
        assert reloaded.validate(value) == schema.validate(value)


def test_one_of_pipeline_paths_and_final_semantics() -> None:
    schema = OutputSchema(
        "one_of",
        one_of=(
            OutputSchema(
                "object",
                properties={"a": OutputSchema("string")},
                required=("a",),
                additional_properties=True,
            ),
            OutputSchema(
                "object",
                properties={"b": OutputSchema("string")},
                required=("b",),
                additional_properties=True,
            ),
        ),
    )

    class Change:
        def check(self, value: Any, context: RuleContext) -> RuleResult:
            if value == {"a": "x", "b": "y"}:
                return RuleResult(True)
            return RuleResult(False, "change", "change a", {"a": "x", "b": "y"})

    pipeline = ValidationPipeline(schema, (RuleBinding("change", (), Change(), "fix"),))
    report = pipeline.validate({"a": "x"})
    assert not report.valid
    assert report.output is None
    assert any(issue.code == "schema_one_of" for issue in report.issues)
    closed = OutputSchema(
        "one_of",
        one_of=(
            OutputSchema("object", properties={"a": OutputSchema("string")}),
            OutputSchema("object", properties={"b": OutputSchema("string")}),
        ),
    )
    with pytest.raises(ValueError, match="incompatible"):
        ValidationPipeline(closed, (RuleBinding("typo", ("missing",), Change()),))
    assert (
        ValidationPipeline(schema, (RuleBinding("a", ("a",), StringChoices(("x",))),))
        .validate({"b": "y"})
        .valid
    )


def test_one_of_generation_feedback_keeps_declared_paths_but_redacts_dynamic() -> None:
    class Provider:
        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            return GeneratedResponse('{"label":"bad","private-key":"bad"}', TokenUsage(0, 0))

    schema = OutputSchema(
        "one_of",
        one_of=(
            OutputSchema(
                "object",
                properties={"label": OutputSchema("string")},
                required=("label",),
                additional_properties=OutputSchema("string"),
            ),
            OutputSchema(
                "object", properties={"other": OutputSchema("integer")}, required=("other",)
            ),
        ),
    )
    pipeline = ValidationPipeline(
        schema,
        (
            RuleBinding("declared", ("label",), StringChoices(("ok",))),
            RuleBinding("dynamic", ("private-key",), StringChoices(("ok",))),
        ),
    )
    report = asyncio.run(
        AsyncGenerationRunner(pipeline, Provider(), GenerationPolicy(max_attempts=1)).run("go")
    )
    assert [item.path for item in report.attempts[0].feedback] == ['$["label"]', "$"]
    assert "private-key" not in str(report.to_dict())


def test_one_of_config_roundtrip_and_cli(tmp_path: Any) -> None:
    from io import BytesIO, StringIO, TextIOWrapper

    schema = OutputSchema("one_of", one_of=(OutputSchema("number"), OutputSchema("integer")))
    pipeline = load_output_config(export_output_config(ValidationPipeline(schema)))
    assert pipeline.schema.kind == "one_of"
    assert pipeline.validate(1.5).valid
    assert [issue.code for issue in pipeline.validate(1).issues] == ["schema_one_of"]
    config_path = tmp_path / "oneof.json"
    config_path.write_text(json.dumps(export_output_config(pipeline)), encoding="utf-8")
    for raw, accepted in ((b"1.5", True), (b"1", False)):
        stdin = TextIOWrapper(BytesIO(raw), encoding="ascii")
        stdout, stderr = StringIO(), StringIO()
        status = run(
            ["validate-output", str(config_path), "-"], stdin=stdin, stdout=stdout, stderr=stderr
        )
        assert status == (0 if accepted else 1)
        assert stderr.getvalue() == ""
        result = json.loads(stdout.getvalue())
        assert result["valid"] is accepted
        if not accepted:
            assert [issue["code"] for issue in result["issues"]] == ["schema_one_of"]
