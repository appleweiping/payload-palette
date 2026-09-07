from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from examples.validate_model_output import run_example

from payload_palette import (
    JSONValue,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    PayloadValidationError,
    RuleBinding,
    RuleContext,
    RuleResult,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
)


@dataclass
class Callback:
    callback: Callable[[JSONValue, RuleContext], Any]

    def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
        return self.callback(value, context)


def fail(value: JSONValue, context: RuleContext) -> RuleResult:
    return RuleResult(False, "denied", "not acceptable")


def test_model_output_example_repairs_and_filters_with_final_verification() -> None:
    result = run_example()
    assert result["valid"] is True
    assert result["output"] == {"summary": "A red bicycle.", "confidence": 0.8, "category": "image"}
    assert result["invocations"] == 7
    assert [item["status"] for item in result["outcomes"]] == [
        "fixed",
        "fixed",
        "filtered",
        "passed",
        "passed",
    ]


def test_rejected_report_never_exposes_partially_repaired_output() -> None:
    pipeline = ValidationPipeline(
        OutputSchema("string"), (RuleBinding("deny", (), Callback(fail)),)
    )
    report = pipeline.validate("secret")
    assert report.valid is False
    assert report.output is None
    assert report.issues[0].code == "denied"
    assert "secret" not in str(report.to_dict())
    assert "output" not in report.to_dict()
    assert report.to_dict(include_output=True)["output"] is None


def test_no_callback_mutation_reaches_input_output_or_other_callbacks() -> None:
    seen: list[JSONValue] = []

    def malicious(value: JSONValue, context: RuleContext) -> RuleResult:
        assert isinstance(value, dict)
        value["n"] = 100
        return RuleResult(True)

    def observer(value: JSONValue, context: RuleContext) -> RuleResult:
        seen.append(value)
        return RuleResult(True)

    original: dict[str, JSONValue] = {"n": 1}
    pipeline = ValidationPipeline(
        OutputSchema("object", additional_properties=True),
        (
            RuleBinding("mutate", (), Callback(malicious)),
            RuleBinding("observe", (), Callback(observer)),
        ),
    )
    report = pipeline.validate(original)
    assert original == {"n": 1}
    assert seen == [{"n": 1}]
    returned = report.output
    assert isinstance(returned, dict)
    returned.clear()
    original.clear()
    assert report.output == {"n": 1}


def test_fix_is_copied_and_rechecked_and_array_paths_are_resolved() -> None:
    fixed: list[JSONValue] = [1]

    def callback(value: JSONValue, context: RuleContext) -> RuleResult:
        return RuleResult(True) if value == [1] else RuleResult(False, "repair", "replace", fixed)

    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("array", items=OutputSchema("integer"))),
        (RuleBinding("replace", (0,), Callback(callback), "fix"),),
    )
    report = pipeline.validate([[2]])
    fixed.append(2)
    assert report.valid
    assert report.output == [[1]]
    assert report.invocations == 3


def test_later_fix_invalidating_earlier_rule_is_rejected() -> None:
    def must_be_lower(value: JSONValue, context: RuleContext) -> RuleResult:
        return RuleResult(True) if value == "a" else RuleResult(False, "lowercase", "expected a")

    pipeline = ValidationPipeline(
        OutputSchema("string"),
        (
            RuleBinding("lower", (), Callback(must_be_lower)),
            RuleBinding("upper", (), StringChoices(("A",), fix_case=True), "fix"),
        ),
    )
    report = pipeline.validate("a")
    assert not report.valid
    assert report.issues[0].code == "lowercase"
    assert report.outcomes[-2].phase == "final"


def test_filtering_required_property_and_type_breaking_fix_fail_schema() -> None:
    schema = OutputSchema("object", properties={"n": OutputSchema("integer")}, required=("n",))
    report = ValidationPipeline(
        schema, (RuleBinding("remove", ("n",), Callback(fail), "filter"),)
    ).validate({"n": 1})
    assert not report.valid
    assert report.issues[0].code == "schema_required"

    def fix(value: JSONValue, context: RuleContext) -> RuleResult:
        return (
            RuleResult(True) if value == "x" else RuleResult(False, "replace", "replacement", "x")
        )

    report = ValidationPipeline(
        schema, (RuleBinding("replace", ("n",), Callback(fix), "fix"),)
    ).validate({"n": 1})
    assert not report.valid
    assert report.issues[0].code == "schema_type"


def test_missing_optional_path_skips_and_filtering_stops_later_field_rules() -> None:
    schema = OutputSchema(
        "object",
        properties={
            "absent": OutputSchema("object", properties={"nested": OutputSchema("string")}),
            "extra": OutputSchema("string"),
        },
    )
    pipeline = ValidationPipeline(
        schema,
        (
            RuleBinding("optional", ("absent", "nested"), Callback(fail)),
            RuleBinding("remove", ("extra",), Callback(fail), "filter"),
            RuleBinding("after_remove", ("extra",), Callback(fail)),
        ),
    )
    report = pipeline.validate({"extra": "unused"})
    assert report.valid
    assert report.output == {}
    assert report.invocations == 1


@pytest.mark.parametrize(
    ("schema", "path"),
    [
        (
            OutputSchema("object", properties={"age": OutputSchema("integer")}, required=("age",)),
            ("ag",),
        ),
        (OutputSchema("integer"), ("child",)),
        (
            OutputSchema(
                "object", properties={"age": OutputSchema("integer")}, additional_properties=True
            ),
            ("age", "child"),
        ),
        (OutputSchema("object", additional_properties=True), (0,)),
        (OutputSchema("array", items=OutputSchema("string")), ("0",)),
        (OutputSchema("array", items=OutputSchema("string")), (0, "child")),
        (OutputSchema("array", items=OutputSchema("object")), (0, "typo")),
        (OutputSchema("array", items=OutputSchema("string"), max_length=2), (2,)),
    ],
)
def test_impossible_rule_paths_are_configuration_errors(schema: OutputSchema, path: Any) -> None:
    with pytest.raises(ValueError, match="rule 'adult'"):
        ValidationPipeline(schema, (RuleBinding("adult", path, Callback(fail)),))


def test_possible_optional_fields_and_absent_array_positions_remain_skips() -> None:
    schema = OutputSchema(
        "object",
        properties={
            "items": OutputSchema(
                "array", items=OutputSchema("object", properties={"name": OutputSchema("string")})
            )
        },
    )
    pipeline = ValidationPipeline(
        schema, (RuleBinding("name", ("items", 3, "name"), Callback(fail)),)
    )
    for document in ({}, {"items": []}, {"items": [{}, {}, {}, {}]}):
        report = pipeline.validate(document)
        assert report.valid
        assert report.invocations == 0
    report = pipeline.validate({"items": [{}, {}, {}, {"name": "denied"}]})
    assert not report.valid
    assert report.invocations == 1


def test_undeclared_open_object_paths_allow_unknown_shape_but_validate_when_present() -> None:
    pipeline = ValidationPipeline(
        OutputSchema("object", additional_properties=True),
        (RuleBinding("unknown", ("extension", 0, "label"), Callback(fail)),),
    )
    for document in ({}, {"extension": None}, {"extension": []}, {"extension": [None]}):
        assert pipeline.validate(document).valid
    report = pipeline.validate({"extension": [{"label": "denied"}]})
    assert not report.valid
    assert report.invocations == 1


def test_schema_errors_stop_callbacks_and_duplicate_json_is_rejected() -> None:
    invoked: list[bool] = []
    pipeline = ValidationPipeline(
        OutputSchema("integer"),
        (RuleBinding("never", (), Callback(lambda *_: invoked.append(True))),),
    )
    assert not pipeline.validate(True).valid
    assert not invoked
    with pytest.raises(PayloadValidationError, match="duplicate_json_key"):
        pipeline.validate_json_bytes(b'{"x":1,"x":2}')
    with pytest.raises(PayloadValidationError, match="input_too_large"):
        pipeline.validate_json_bytes(b"123", max_input_bytes=2)


@pytest.mark.parametrize("result", [None, True, {}, object()])
def test_malformed_callback_results_fail_closed(result: Any) -> None:
    pipeline = ValidationPipeline(
        OutputSchema("null"), (RuleBinding("bad", (), Callback(lambda *_: result)),)
    )
    report = pipeline.validate(None)
    assert not report.valid
    assert report.issues[0].code == "validator_contract"


def test_raised_callback_errors_do_not_leak_exception_secrets() -> None:
    def raising(*args: Any) -> RuleResult:
        raise RuntimeError("API_KEY=this_is_sensitive")

    report = ValidationPipeline(
        OutputSchema("null"), (RuleBinding("bad", (), Callback(raising)),)
    ).validate(None)
    assert report.issues[0].code == "validator_contract"
    assert "API_KEY" not in str(report.to_dict())


def test_accidental_coroutine_return_is_closed_and_rejected() -> None:
    async def asynchronous() -> RuleResult:
        return RuleResult(True)

    report = ValidationPipeline(
        OutputSchema("null"), (RuleBinding("bad", (), Callback(lambda *_: asynchronous())),)
    ).validate(None)
    assert report.issues[0].code == "validator_contract"


def test_invalid_and_unsuccessful_fixes_never_enter_output() -> None:
    for fix in (object(), "oversized"):
        report = ValidationPipeline(
            OutputSchema("null"),
            (
                RuleBinding(
                    "bad",
                    (),
                    Callback(lambda *_, fix=fix: RuleResult(False, "bad", "bad", fix)),
                    "fix",
                ),
            ),
            OutputLimits(max_characters=3),
        ).validate(None)
        assert report.issues[0].code == "invalid_fix"
    report = ValidationPipeline(
        OutputSchema("null"), (RuleBinding("none", (), Callback(fail), "fix"),)
    ).validate(None)
    assert report.issues[0].code == "denied"
    report = ValidationPipeline(
        OutputSchema("null"),
        (
            RuleBinding(
                "loop", (), Callback(lambda *_: RuleResult(False, "bad", "bad", None)), "fix"
            ),
        ),
    ).validate(None)
    assert report.invocations == 2
    assert report.issues[0].code == "bad"


def test_fix_aggregate_budget_and_callback_budget_cannot_be_bypassed() -> None:
    def expand(value: JSONValue, context: RuleContext) -> RuleResult:
        return (
            RuleResult(True) if value == "long" else RuleResult(False, "expand", "expand", "long")
        )

    pipeline = ValidationPipeline(
        OutputSchema("object", additional_properties=True),
        (
            RuleBinding("a", ("a",), Callback(expand), "fix"),
            RuleBinding("b", ("b",), Callback(expand), "fix"),
        ),
        OutputLimits(max_characters=8),
    )
    report = pipeline.validate({"a": "x", "b": "x"})
    assert not report.valid
    assert report.issues[0].code == "output_budget"
    limited = ValidationPipeline(
        OutputSchema("string"),
        (RuleBinding("trim", (), TrimmedString(), "fix"),),
        OutputLimits(max_invocations=1),
    )
    report = limited.validate(" x ")
    assert not report.valid
    assert report.invocations == 1
    assert report.issues[0].code == "validator_budget"
    limited = ValidationPipeline(
        OutputSchema("string"),
        (RuleBinding("trim", (), TrimmedString(), "fix"),),
        OutputLimits(max_invocations=2),
    )
    assert limited.validate(" x ").issues[0].code == "validator_budget"


def test_rule_failures_and_invocations_have_a_bound() -> None:
    rules = tuple(RuleBinding(f"rule_{n}", (), Callback(fail)) for n in range(5))
    report = ValidationPipeline(
        OutputSchema("null"), rules, OutputLimits(max_issues=1, max_invocations=2)
    ).validate(None)
    assert not report.valid
    assert len(report.issues) == 1
    assert report.invocations == 2
    assert len(report.outcomes) == 3


def test_input_resource_failure_occurs_before_callback() -> None:
    pipeline = ValidationPipeline(
        OutputSchema("string"),
        (RuleBinding("never", (), Callback(fail)),),
        OutputLimits(max_characters=1),
    )
    with pytest.raises(OutputContractError):
        pipeline.validate("too long")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"valid": 1},
        {"valid": False},
        {"valid": False, "code": "UPPER"},
        {"valid": False, "code": 1},
        {"valid": False, "code": "bad", "message": "x" * 2049},
        {"valid": True, "code": "bad"},
        {"valid": True, "fix": None},
    ],
)
def test_rule_result_contract(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RuleResult(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"rule_id": "BAD"},
        {"path": []},
        {"path": tuple(range(33))},
        {"path": (True,)},
        {"path": (-1,)},
        {"path": ("\ud800",)},
        {"on_fail": "noop"},
        {"on_fail": "filter"},
        {"on_fail": "filter", "path": (0,)},
        {"validator": object()},
    ],
)
def test_rule_binding_configuration(kwargs: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {"rule_id": "rule", "path": (), "validator": Callback(fail)}
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        RuleBinding(**arguments)


def test_async_validator_is_explicitly_rejected() -> None:
    class AsyncValidator:
        async def check(self, value: JSONValue, context: RuleContext) -> RuleResult:
            return RuleResult(True)

    with pytest.raises(ValueError, match="synchronous"):
        RuleBinding("async", (), AsyncValidator())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs", [{"schema": None}, {"rules": []}, {"rules": (object(),)}, {"limits": None}]
)
def test_pipeline_configuration(kwargs: dict[str, Any]) -> None:
    arguments: dict[str, Any] = {"schema": OutputSchema("null")}
    arguments.update(kwargs)
    with pytest.raises(ValueError):
        ValidationPipeline(**arguments)


def test_duplicate_rules_and_excessive_rule_count_rejected() -> None:
    rule = RuleBinding("same", (), Callback(fail))
    with pytest.raises(ValueError, match="unique"):
        ValidationPipeline(OutputSchema("null"), (rule, rule))
    with pytest.raises(ValueError, match="256"):
        ValidationPipeline(OutputSchema("null"), (rule,) * 257)


@pytest.mark.parametrize(
    "choices", [[], (), ("a", "a"), ("\ud800",), (1,), tuple(str(n) for n in range(257))]
)
def test_string_choices_configuration(choices: Any) -> None:
    with pytest.raises(ValueError):
        StringChoices(choices)


def test_builtin_validator_semantics_and_ambiguous_case_fix() -> None:
    context = RuleContext("test", (), "initial")
    assert TrimmedString().check(1, context).code == "string_required"
    assert StringChoices(("x",)).check(1, context).code == "string_choice"
    pipeline = ValidationPipeline(
        OutputSchema("string"),
        (RuleBinding("case", (), StringChoices(("ss", "SS"), fix_case=True), "fix"),),
    )
    assert not pipeline.validate("Ss").valid
    assert pipeline.validate("ss").valid
