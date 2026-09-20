"""Bounded per-item semantic validation on declared homogeneous arrays."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, replace
from typing import Any

import pytest

from payload_palette import (
    ArrayRuleBinding,
    AsyncGenerationRunner,
    AsyncValidationPipeline,
    GeneratedResponse,
    GenerationFeedback,
    GenerationRequest,
    IncrementalOutputSession,
    OutputConfigError,
    OutputLimits,
    OutputSchema,
    RuleBinding,
    RuleContext,
    RuleResult,
    StringChoices,
    TokenUsage,
    TrimmedString,
    ValidationPipeline,
    export_output_config,
)


class _Accept:
    def check(self, value: object, context: object) -> RuleResult:
        return RuleResult(True)


def test_array_rule_public_api_visits_unknown_length_array() -> None:
    schema = OutputSchema("array", items=OutputSchema("string"))
    pipeline = ValidationPipeline(schema, (ArrayRuleBinding("every_item", (), (), _Accept()),))
    report = pipeline.validate(["first", "second", "third"])
    assert report.valid
    assert report.invocations == 3
    assert [outcome.path for outcome in report.outcomes] == ["$[0]", "$[1]", "$[2]"]


def _prediction_schema(*, required: bool = False) -> OutputSchema:
    item = OutputSchema(
        "object",
        properties={"label": OutputSchema("string")},
        required=("label",) if required else (),
    )
    return OutputSchema(
        "object",
        properties={"predictions": OutputSchema("array", items=item)},
        required=("predictions",),
    )


@pytest.mark.parametrize("seed", range(12))
def test_seeded_per_item_trim_matches_handwritten_oracle(seed: int) -> None:
    rng = random.Random(seed)
    labels = [rng.choice((" a ", "b", " c", " é ", "", None)) for _ in range(rng.randrange(9))]
    input_items = [{} if label is None else {"label": label} for label in labels]
    original = {"predictions": input_items}
    pipeline = ValidationPipeline(
        _prediction_schema(),
        (ArrayRuleBinding("trim_labels", ("predictions",), ("label",), TrimmedString(), "fix"),),
    )
    report = pipeline.validate(original)

    # A separate direct loop determines all expected values, phases and paths;
    # it does not use the proposed dispatch helper or a second pipeline.
    expected_items = []
    initial = []
    final = []
    calls = 0
    changed = False
    for index, label in enumerate(labels):
        if label is None:
            expected_items.append({})
            continue
        path = f'$["predictions"][{index}]["label"]'
        trimmed = label.strip()
        if trimmed != label:
            initial.append((path, "repair", "fixed"))
            calls += 2
            changed = True
        else:
            initial.append((path, "initial", "passed"))
            calls += 1
        expected_items.append({"label": trimmed})
        final.append((path, "final", "passed"))
    if changed:
        calls += len(final)
    assert report.valid
    assert report.output == {"predictions": expected_items}
    assert report.invocations == calls
    assert [(o.path, o.phase, o.status) for o in report.outcomes] == initial + (
        final if changed else []
    )
    assert original == {"predictions": input_items}
    returned = report.output
    assert isinstance(returned, dict)
    returned.clear()
    assert report.output == {"predictions": expected_items}


@dataclass
class _Trace:
    seen: list[tuple[tuple[str | int, ...], str, object]]

    def check(self, value: object, context: RuleContext) -> RuleResult:
        self.seen.append((context.path, context.phase, value))
        return RuleResult(True)


def test_shared_budget_stops_before_over_limit_array_sibling() -> None:
    trace = _Trace([])
    schema = OutputSchema("array", items=OutputSchema("string"))
    pipeline = ValidationPipeline(
        schema,
        (RuleBinding("root", (), trace), ArrayRuleBinding("each", (), (), trace)),
        OutputLimits(max_invocations=2),
    )
    report = pipeline.validate(["a", "b", "c"])
    assert not report.valid and report.output is None
    assert report.invocations == 2
    assert trace.seen == [((), "initial", ["a", "b", "c"]), ((0,), "initial", "a")]
    assert [(o.path, o.code) for o in report.outcomes] == [
        ("$", ""),
        ("$[0]", ""),
        ("$[1]", "validator_budget"),
    ]


def test_fanout_rejects_before_callback_without_leaking_items() -> None:
    trace = _Trace([])
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("each", (), (), trace, max_items=2),),
    )
    assert pipeline.validate(["secret-a", "secret-b"]).valid
    trace.seen.clear()
    report = pipeline.validate(["secret-a", "secret-b", "secret-c"])
    assert not report.valid and report.output is None and report.invocations == 0
    assert trace.seen == []
    assert [(o.path, o.phase, o.code) for o in report.outcomes] == [
        ("$", "initial", "validator_fanout")
    ]
    assert "secret" not in str(report.to_dict())


def test_optional_array_absence_skips_all_callbacks() -> None:
    trace = _Trace([])
    pipeline = ValidationPipeline(
        OutputSchema(
            "object", properties={"optional": OutputSchema("array", items=OutputSchema("string"))}
        ),
        (ArrayRuleBinding("each", ("optional",), (), trace),),
    )
    report = pipeline.validate({})
    assert report.valid and report.output == {}
    assert report.invocations == 0 and report.outcomes == ()
    assert trace.seen == []


@pytest.mark.parametrize("count", (9_999, 10_000, 10_001))
def test_hard_array_fanout_boundary(count: int) -> None:
    class Counting:
        def __init__(self) -> None:
            self.calls = 0

        def check(self, value: object, context: RuleContext) -> RuleResult:
            self.calls += 1
            return RuleResult(True)

    checker = Counting()
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("each", (), (), checker, max_items=10_000),),
        OutputLimits(max_nodes=100_000, max_invocations=10_000),
    )
    report = pipeline.validate(["x"] * count)
    if count > 10_000:
        assert not report.valid and report.output is None
        assert report.invocations == checker.calls == 0
        assert [(o.path, o.code) for o in report.outcomes] == [("$", "validator_fanout")]
    else:
        assert report.valid and report.invocations == checker.calls == count
        assert len(report.outcomes) == count


def test_later_sibling_rejects_after_private_fix_without_partial_output() -> None:
    class _FixOrReject:
        def check(self, value: object, context: RuleContext) -> RuleResult:
            if value == " bad ":
                return RuleResult(False, "spaces", "trim", "bad")
            if value == "reject":
                return RuleResult(False, "denied", "denied")
            return RuleResult(True)

    input_value = [" bad ", "reject"]
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("each", (), (), _FixOrReject(), "fix"),),
    )
    report = pipeline.validate(input_value)
    assert not report.valid and report.output is None
    assert input_value == [" bad ", "reject"]
    assert report.invocations == 3
    assert [(o.path, o.status) for o in report.outcomes] == [
        ("$[0]", "fixed"),
        ("$[1]", "rejected"),
    ]


def test_final_verification_catches_later_exact_repair() -> None:
    class _Lower:
        def check(self, value: object, context: RuleContext) -> RuleResult:
            return RuleResult(True) if value == "a" else RuleResult(False, "lower", "expected a")

    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (
            ArrayRuleBinding("lower", (), (), _Lower()),
            RuleBinding("upper", (0,), StringChoices(("A",), fix_case=True), "fix"),
        ),
    )
    report = pipeline.validate(["a"])
    assert not report.valid and report.output is None
    assert report.issues[0].code == "lower"
    assert [(o.rule_id, o.phase, o.status) for o in report.outcomes] == [
        ("lower", "initial", "passed"),
        ("upper", "repair", "fixed"),
        ("lower", "final", "rejected"),
        ("upper", "final", "passed"),
    ]


@pytest.mark.parametrize("required", (False, True))
def test_filter_only_removes_item_object_member(required: bool) -> None:
    class _Deny:
        def check(self, value: object, context: RuleContext) -> RuleResult:
            return RuleResult(False, "denied", "remove")

    pipeline = ValidationPipeline(
        _prediction_schema(required=required),
        (ArrayRuleBinding("drop_label", ("predictions",), ("label",), _Deny(), "filter"),),
    )
    items = [{"label": "secret"}] if required else [{"label": "secret"}, {}]
    report = pipeline.validate({"predictions": items})
    assert report.valid == (not required)
    assert report.output == ({"predictions": [{}, {}]} if not required else None)
    assert [o.path for o in report.outcomes] == ['$["predictions"][0]["label"]']
    if required:
        assert report.issues[0].code == "schema_required"


def test_root_item_and_nested_declared_array_targets() -> None:
    trace = _Trace([])
    tag_array = OutputSchema("array", items=OutputSchema("string"))
    schema = OutputSchema(
        "object",
        properties={
            "groups": OutputSchema(
                "array", items=OutputSchema("object", properties={"tags": tag_array})
            )
        },
    )
    pipeline = ValidationPipeline(
        schema, (ArrayRuleBinding("tags", ("groups", 0, "tags"), (), trace),)
    )
    assert pipeline.validate({"groups": [{"tags": ["a", "b"]}]}).valid
    assert [path for path, _, _ in trace.seen] == [
        ("groups", 0, "tags", 0),
        ("groups", 0, "tags", 1),
    ]


def test_array_rule_matches_explicit_fixed_index_bindings_for_known_length() -> None:
    schema = OutputSchema("array", items=OutputSchema("string"))
    values = [" A ", "B", " C"]
    array_report = ValidationPipeline(
        schema, (ArrayRuleBinding("each", (), (), TrimmedString(), "fix"),)
    ).validate(values)
    exact_report = ValidationPipeline(
        schema,
        tuple(RuleBinding(f"item_{index}", (index,), TrimmedString(), "fix") for index in range(3)),
    ).validate(values)
    assert array_report.valid == exact_report.valid
    assert array_report.output == exact_report.output == ["A", "B", "C"]
    assert array_report.invocations == exact_report.invocations
    assert [(o.path, o.phase, o.status) for o in array_report.outcomes] == [
        (o.path, o.phase, o.status) for o in exact_report.outcomes
    ]


def test_aggregate_fix_growth_is_rejected_at_second_item() -> None:
    class _Double:
        def check(self, value: object, context: RuleContext) -> RuleResult:
            if type(value) is str and len(value) == 1:
                return RuleResult(False, "short", "double", value * 2)
            return RuleResult(True)

    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("double", (), (), _Double(), "fix"),),
        OutputLimits(max_characters=3),
    )
    report = pipeline.validate(["a", "b"])
    assert not report.valid and report.output is None
    assert report.issues[0].code == "output_budget"
    assert report.issues[0].path == "$[1]"


def test_invalidated_array_shape_is_left_for_final_schema_report() -> None:
    class _BreakArray:
        def check(self, value: object, context: RuleContext) -> RuleResult:
            return (
                RuleResult(False, "replace", "replace", "not-an-array")
                if type(value) is list
                else RuleResult(True)
            )

    trace = _Trace([])
    schema = OutputSchema(
        "object", properties={"items": OutputSchema("array", items=OutputSchema("string"))}
    )
    pipeline = ValidationPipeline(
        schema,
        (
            RuleBinding("break_array", ("items",), _BreakArray(), "fix"),
            ArrayRuleBinding("each", ("items",), (), trace),
        ),
    )
    report = pipeline.validate({"items": ["a"]})
    assert not report.valid and report.output is None
    assert report.invocations == 2
    assert trace.seen == []
    assert any(issue.code == "schema_type" for issue in report.issues)


def test_async_composition_and_final_incremental_validation() -> None:
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("trim", (), (), TrimmedString(), "fix"),),
    )
    async_report = asyncio.run(AsyncValidationPipeline(pipeline).validate([" a ", "b"]))
    assert async_report.valid and async_report.output == ["a", "b"]
    session = IncrementalOutputSession(pipeline)
    session.feed(b'[" a ",')
    session.feed(b'"b"]')
    final = session.finish()
    assert final.report.valid and final.report.output == ["a", "b"]
    assert final.report.invocations == 5


def test_generation_feedback_contains_declared_index_not_rejected_value() -> None:
    class _DenyBad:
        def check(self, value: object, context: RuleContext) -> RuleResult:
            return (
                RuleResult(False, "denied", "bad item")
                if value == "private-bad"
                else RuleResult(True)
            )

    class _Provider:
        def __init__(self) -> None:
            self.requests: list[GenerationRequest] = []

        async def generate(self, request: GenerationRequest) -> GeneratedResponse:
            self.requests.append(request)
            text = '["ok","private-bad"]' if len(self.requests) == 1 else '["ok","good"]'
            return GeneratedResponse(text, TokenUsage(1, 1))

    provider = _Provider()
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("deny", (), (), _DenyBad()),),
    )
    report = asyncio.run(AsyncGenerationRunner(pipeline, provider).run("Check items"))
    assert report.valid and report.output == ["ok", "good"]
    assert provider.requests[1].feedback == (GenerationFeedback("denied", "$[1]"),)
    assert "private-bad" not in str(report.to_dict())


def test_invalid_selector_shapes_and_config_export_refusal() -> None:
    item = OutputSchema("string")
    array = OutputSchema("array", items=item)
    positional = OutputSchema("array", prefix_items=(item,))
    for schema, array_path in (
        (positional, ()),
        (OutputSchema("object", additional_properties=True), ("dynamic",)),
        (OutputSchema("union", any_of=(array, OutputSchema("string"))), ()),
    ):
        with pytest.raises(ValueError, match="array item path"):
            ValidationPipeline(schema, (ArrayRuleBinding("each", array_path, (), _Accept()),))
    with pytest.raises(ValueError, match="filter requires"):
        ArrayRuleBinding("bad", (), (), _Accept(), "filter")
    with pytest.raises(ValueError, match="max_items"):
        ArrayRuleBinding("bad", (), (), _Accept(), max_items=True)
    with pytest.raises(TypeError, match="positional"):
        ArrayRuleBinding("bad", (), (), _Accept(), "reject", 5)  # type: ignore[call-arg]
    pipeline = ValidationPipeline(array, (ArrayRuleBinding("each", (), (), _Accept()),))
    with pytest.raises(OutputConfigError, match="exact rule bindings"):
        export_output_config(pipeline)


def test_forged_public_declarations_reject_without_hostile_hooks() -> None:
    class Hostile:
        calls = 0

        def __hash__(self) -> int:
            self.calls += 1
            raise RuntimeError("hash must not run")

        def __eq__(self, other: object) -> bool:
            self.calls += 1
            raise RuntimeError("equality must not run")

        def __len__(self) -> int:
            self.calls += 1
            raise RuntimeError("length must not run")

    class TupleSubclass(tuple[Any, ...]):
        calls = 0

        def __len__(self) -> int:
            self.calls += 1
            raise RuntimeError("length must not run")

    binding = ArrayRuleBinding("each", (), (), _Accept())
    hostile = Hostile()
    for field, value in (
        ("array_path", TupleSubclass()),
        ("array_path", (hostile,)),
        ("max_items", hostile),
        ("on_fail", hostile),
    ):
        object.__setattr__(binding, field, value)
        with pytest.raises(ValueError):
            ValidationPipeline(OutputSchema("array", items=OutputSchema("string")), (binding,))
        restored = () if field == "array_path" else 1000 if field == "max_items" else "reject"
        object.__setattr__(binding, field, restored)
    object.__setattr__(binding, "rule_id", hostile)
    with pytest.raises(ValueError):
        ValidationPipeline(OutputSchema("array", items=OutputSchema("string")), (binding,))
    object.__setattr__(binding, "rule_id", "each")
    assert hostile.calls == 0
    assert TupleSubclass.calls == 0


def test_forged_exact_rule_path_rejected_before_length_hook() -> None:
    class HostileTuple(tuple[Any, ...]):
        calls = 0

        def __len__(self) -> int:
            self.calls += 1
            raise RuntimeError("tuple subclass length must not run")

    binding = RuleBinding("exact", (), _Accept())
    object.__setattr__(binding, "path", HostileTuple())
    with pytest.raises(ValueError, match="path"):
        ValidationPipeline(OutputSchema("string"), (binding,))
    assert HostileTuple.calls == 0


def test_forged_array_item_schema_rejected_at_pipeline_construction() -> None:
    schema = OutputSchema("array", items=OutputSchema("string"))
    object.__setattr__(schema, "items", 123)
    with pytest.raises(ValueError, match=r"array.*schema|schema.*array"):
        ValidationPipeline(schema, (ArrayRuleBinding("each", (), (), _Accept()),))


def test_pipeline_clone_does_not_replay_array_validator_lookup() -> None:
    class Lookup:
        def __init__(self) -> None:
            self.lookups = 0
            self.broken = False

        @property
        def check(self):
            self.lookups += 1
            if self.broken:
                raise RuntimeError("validator lookup must not be replayed")

            def valid(value: object, context: object) -> RuleResult:
                return RuleResult(True)

            return valid

    checker = Lookup()
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("string")),
        (ArrayRuleBinding("each", (), (), checker),),
    )
    admitted = checker.lookups
    checker.broken = True
    assert replace(pipeline).rules == pipeline.rules
    assert checker.lookups == admitted
