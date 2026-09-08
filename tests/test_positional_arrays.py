import asyncio
import json
import typing
from dataclasses import dataclass, field, make_dataclass
from datetime import date
from decimal import Decimal
from itertools import product
from typing import Annotated, NamedTuple, TypedDict

import pytest

from payload_palette import (
    AnnotationAdapter,
    AsyncGenerationRunner,
    AsyncRuleBinding,
    AsyncValidationPipeline,
    DataclassAdapter,
    FieldConstraints,
    GeneratedResponse,
    GenerationFeedback,
    IncrementalOutputSession,
    OutputContractError,
    OutputLimits,
    OutputSchema,
    PayloadValidationError,
    RuleBinding,
    RuleResult,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    TokenUsage,
    TrimmedString,
    ValidationPipeline,
    export_output_schema,
    load_output_schema,
)

# Look up the deprecated alias as test data, not as our annotation style.
LEGACY_TUPLE = vars(typing)["Tuple"]


def test_closed_prefix_checks_each_present_position_without_implying_required_length():
    schema = OutputSchema("array", prefix_items=(OutputSchema("integer"), OutputSchema("string")))
    assert not schema.validate([])
    assert not schema.validate([1])
    assert not schema.validate([1, "ok"])
    assert schema.validate(["wrong"])[0].path == "$[0]"
    assert schema.validate([1, "ok", 3])[0].path == "$[2]"


def test_imported_prefix_and_typed_suffix_have_independent_roles():
    schema = load_output_schema(
        {
            "type": "array",
            "prefixItems": [{"type": "string"}],
            "items": {"type": "boolean"},
        }
    )
    assert not schema.validate(["name", True, False])
    assert schema.validate([True])[0].path == "$[0]"
    assert schema.validate(["name", 1])[0].path == "$[1]"


@pytest.mark.parametrize(
    ("annotation", "wire", "expected"),
    [
        (tuple[int, str], [3, "three"], (3, "three")),
        (tuple[int, ...], [1, 2], (1, 2)),
        (tuple[()], [], ()),
    ],
)
def test_tuple_annotation_json_and_typed_dataclass_views(annotation, wire, expected):
    assert AnnotationAdapter(annotation).validate_python(wire) == wire
    model = make_dataclass("Record", [("value", annotation)])
    adapter = DataclassAdapter(model)
    record = adapter.validate_python({"value": wire})
    assert type(record.value) is tuple and record.value == expected
    assert adapter.dump_python(record) == {"value": wire}


def test_independent_index_and_length_oracle_for_all_small_arrays():
    types = {"integer": int, "boolean": bool, "string": str}
    arrays = [list(items) for size in range(5) for items in product((0, True, "x"), repeat=size)]
    for names in ((), ("integer",), ("boolean", "string"), ("string", "integer", "boolean")):
        for suffix in (None, "integer", "boolean"):
            for minimum, maximum in ((None, None), (1, 3), (0, 0)):
                schema = OutputSchema(
                    "array",
                    prefix_items=tuple(OutputSchema(name) for name in names),
                    items=None if suffix is None else OutputSchema(suffix),
                    min_length=minimum,
                    max_length=maximum,
                )
                imported = load_output_schema(export_output_schema(schema))
                for array in arrays:
                    valid = (minimum is None or len(array) >= minimum) and (
                        maximum is None or len(array) <= maximum
                    )
                    for index, value in enumerate(array):
                        name = names[index] if index < len(names) else suffix
                        valid &= name is not None and type(value) is types[name]
                    assert (not schema.validate(array)) is valid
                    assert (not imported.validate(array)) is valid


@pytest.mark.parametrize(
    "prefix", [[], [OutputSchema("integer")], (None,), (True,), (OutputSchema("null"),) * 257]
)
def test_direct_prefix_requires_exact_bounded_schema_tuple(prefix):
    with pytest.raises(ValueError):
        OutputSchema("array", prefix_items=prefix)


@pytest.mark.parametrize("kind", ["string", "object", "integer", "null"])
def test_positional_constraints_cannot_be_silently_ignored_on_other_kinds(kind):
    with pytest.raises(ValueError):
        OutputSchema(kind, prefix_items=())


@pytest.mark.parametrize("prefix", [[], {}, True, [True], [None], [{"type": "null"}] * 257])
def test_import_prefix_array_is_nonempty_bounded_and_contains_supported_schemas(prefix):
    with pytest.raises(SchemaDefinitionError):
        load_output_schema({"type": "array", "prefixItems": prefix, "items": False})


@pytest.mark.parametrize("items", [True, [], None, "false"])
def test_unrestricted_or_malformed_suffix_schema_is_not_inferred(items):
    with pytest.raises(SchemaDefinitionError):
        load_output_schema({"type": "array", "prefixItems": [{"type": "integer"}], "items": items})
    with pytest.raises(SchemaDefinitionError):
        load_output_schema({"type": "array", "prefixItems": [{"type": "integer"}]})


def test_empty_closed_array_and_existing_homogeneous_wires_roundtrip():
    empty = OutputSchema("array", prefix_items=())
    assert empty.json_schema() == {"type": "array", "items": False}
    assert not load_output_schema(empty.json_schema()).validate([])
    assert empty.validate([None])
    old = OutputSchema("array", items=OutputSchema("string"), min_length=2)
    assert old.json_schema() == {"type": "array", "items": {"type": "string"}, "minItems": 2}
    assert load_output_schema(export_output_schema(old)) == old


def test_prefix_definition_aliases_and_work_are_counted_per_occurrence():
    child = {"type": "string"}
    wire = {"type": "array", "prefixItems": [child, child], "items": False}
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(wire, limits=SchemaDefinitionLimits(max_nodes=2))
    schema = load_output_schema(wire, limits=SchemaDefinitionLimits(max_nodes=3))
    child["type"] = "integer"
    assert not schema.validate(["a", "b"])
    with pytest.raises(OutputContractError, match="work"):
        schema.validate(["a", "b"], limits=OutputLimits(max_schema_steps=2))
    with pytest.raises(OutputContractError, match="work"):
        OutputSchema("array", prefix_items=()).validate(
            [0], limits=OutputLimits(max_schema_steps=1)
        )
    issues = OutputSchema("array", prefix_items=()).validate(
        list(range(20)), limits=OutputLimits(max_issues=2)
    )
    assert len(issues) == 3 and issues[-1].code == "schema_issue_limit"
    wire["prefixItems"] = [wire]
    with pytest.raises(SchemaDefinitionError):
        load_output_schema(wire)


def test_semantic_paths_select_exact_prefix_suffix_and_nested_position_before_callbacks():
    calls = []

    class Validator:
        def check(self, value, context):
            calls.append((value, context.path))
            return RuleResult(True)

    schema = OutputSchema(
        "array",
        prefix_items=(
            OutputSchema("integer"),
            OutputSchema("object", properties={"label": OutputSchema("string")}),
        ),
        items=OutputSchema("boolean"),
        max_length=4,
    )
    pipeline = ValidationPipeline(
        schema,
        (RuleBinding("label", (1, "label"), Validator()), RuleBinding("suffix", (2,), Validator())),
    )
    assert pipeline.validate([5, {"label": "ok"}, True]).valid
    assert calls == [("ok", (1, "label")), (True, (2,))]
    for path in ((0, "label"), (2, "label"), (4,)):
        with pytest.raises(ValueError, match="path"):
            ValidationPipeline(schema, (RuleBinding("bad", path, Validator()),))
    closed = OutputSchema("array", prefix_items=(OutputSchema("integer"),))
    with pytest.raises(ValueError, match="path"):
        ValidationPipeline(closed, (RuleBinding("bad", (1,), Validator()),))
    assert len(calls) == 2


def test_fixed_tuple_nested_dataclasses_scalars_defaults_and_typed_dict_reconstruct():
    calls = []

    @dataclass
    class Child:
        day: date

        def __post_init__(self):
            calls.append("child")

    class Metadata(TypedDict):
        position: tuple[int, str]

    @dataclass
    class Record:
        pair: tuple[Child, Decimal]
        metadata: Metadata
        defaults: tuple[date, ...] = (date(2024, 1, 1),)

    adapter = DataclassAdapter(Record)
    wire = {"pair": [{"day": "2024-02-29"}, "1.00"], "metadata": {"position": [3, "x"]}}
    result = adapter.validate_python(wire)
    assert type(result.pair) is type(result.defaults) is type(result.metadata["position"]) is tuple
    assert result.pair == (Child(date(2024, 2, 29)), Decimal("1.00"))
    assert result.defaults == (date(2024, 1, 1),)
    assert adapter.dump_python(result) == {**wire, "defaults": ["2024-01-01"]}
    assert adapter.validate_json_bytes(adapter.dump_json(result)) == result


def test_bad_later_tuple_member_precedes_every_user_callback():
    calls = []

    @dataclass
    class Child:
        name: str = "default"

        def __post_init__(self):
            calls.append("constructor")

    @dataclass
    class Record:
        pair: tuple[Child, date]
        factory: str = field(default_factory=lambda: calls.append("factory") or "x")

    with pytest.raises(PayloadValidationError):
        DataclassAdapter(Record).validate_python({"pair": [{}, "2023-02-29"]})
    assert calls == []


@pytest.mark.parametrize("annotation", [tuple, LEGACY_TUPLE, tuple[int, str, ...]])
def test_bare_and_unpacked_style_tuple_forms_are_not_inferred(annotation):
    with pytest.raises(SchemaDefinitionError):
        AnnotationAdapter(annotation)


def test_named_tuple_is_not_treated_as_exact_builtin_tuple():
    class Pair(NamedTuple):
        x: int
        y: str

    model = make_dataclass("Record", [("pair", tuple[int, str])])
    with pytest.raises(PayloadValidationError):
        DataclassAdapter(model).dump_python(model(Pair(1, "x")))
    with pytest.raises(SchemaDefinitionError):
        DataclassAdapter(make_dataclass("Named", [("pair", Pair)]))


@pytest.mark.parametrize(
    "annotation", [tuple[()], LEGACY_TUPLE[()], tuple[int], LEGACY_TUPLE[int, str]]
)
def test_legacy_parameterized_tuple_aliases_and_empty_tuple_shape(annotation):
    wire = [] if not annotation.__args__ else [1] if len(annotation.__args__) == 1 else [1, "x"]
    assert AnnotationAdapter(annotation).validate_python(wire) == wire
    with pytest.raises(PayloadValidationError):
        AnnotationAdapter(annotation).validate_python([*wire, "extra"])


def test_tuple_length_constraints_intersect_fixed_shape_and_final_object_is_revalidated():
    model = make_dataclass(
        "Record", [("pair", Annotated[tuple[int, str], FieldConstraints(max_length=5)])]
    )
    adapter = DataclassAdapter(model)
    assert adapter.input_schema.properties["pair"].max_length == 2
    for pair in ((1,), (1, "x", None), [1, "x"]):
        with pytest.raises(PayloadValidationError):
            adapter.dump_python(model(pair))
    with pytest.raises(SchemaDefinitionError):
        AnnotationAdapter(Annotated[tuple[int, str], FieldConstraints(min_length=3)])
    with pytest.raises(OutputContractError):
        AnnotationAdapter(tuple[int, ...]).validate_python((1, 2))


def test_tuple_bearing_wire_union_ambiguity_never_trials_constructors():
    calls = []
    model = make_dataclass(
        "Record",
        [("value", tuple[int, ...] | list[int])],
        namespace={"__post_init__": lambda self: calls.append("constructor")},
    )
    with pytest.raises(PayloadValidationError, match="ambiguous"):
        DataclassAdapter(model).validate_python({"value": [1, 2]})
    assert calls == []
    distinct = make_dataclass("Distinct", [("value", tuple[int, str] | tuple[str, int])])
    adapter = DataclassAdapter(distinct)
    assert adapter.validate_python({"value": [1, "x"]}).value == (1, "x")
    assert adapter.validate_python({"value": ["x", 1]}).value == ("x", 1)


def test_mutable_members_of_literal_tuple_defaults_are_reconstructed_per_call():
    default = ([1],)
    model = make_dataclass("Default", [("value", tuple[list[int]], default)])
    adapter = DataclassAdapter(model)
    default[0].append(2)
    first, second = adapter.validate_python({}), adapter.validate_python({})
    first.value[0].append(3)
    assert first.value == ([1, 3],)
    assert second.value == ([1],)


def test_post_init_tuple_shape_mutation_never_returns_an_accepted_model():
    @dataclass
    class Record:
        value: tuple[int, str]

        def __post_init__(self):
            self.value = (1,)

    with pytest.raises(PayloadValidationError):
        DataclassAdapter(Record).validate_python({"value": [1, "x"]})


def test_tuple_adaptation_never_consumes_arbitrary_iterables_or_custom_copy_hooks():
    calls = []

    class Iterable:
        def __iter__(self):
            calls.append("iteration")
            yield 1

    model = make_dataclass("Record", [("value", tuple[int, ...])])
    adapter = DataclassAdapter(model)
    with pytest.raises(OutputContractError):
        adapter.validate_python({"value": Iterable()})
    with pytest.raises(PayloadValidationError):
        adapter.dump_python(model(Iterable()))
    assert calls == []


def test_prefix_repairs_are_schema_checked_before_async_exact_position_validation():
    calls = []

    class Check:
        async def check(self, value, context):
            calls.append((value, context.path))
            return RuleResult(value == "ready")

    schema = AnnotationAdapter(tuple[int, str]).schema
    sync = ValidationPipeline(schema, (RuleBinding("trim", (1,), TrimmedString(), "fix"),))
    pipeline = AsyncValidationPipeline(sync, (AsyncRuleBinding("ready", (1,), Check()),))
    value = [7, " ready "]
    report = asyncio.run(pipeline.validate(value))
    assert report.valid and report.output == [7, "ready"]
    assert value == [7, " ready "] and calls == [("ready", (1,))]
    for bad in ([7, "ready", False], [7, 5], [7]):
        assert not asyncio.run(pipeline.validate(bad)).valid
    with pytest.raises(ValueError, match="path"):
        AsyncValidationPipeline(sync, (AsyncRuleBinding("bad", (2,), Check()),))
    assert calls == [("ready", (1,))]


def test_all_utf8_byte_splits_keep_positional_semantics_at_explicit_eof_only():
    calls = []

    class Check:
        def check(self, value, context):
            calls.append((value, context.path))
            return RuleResult(True)

    schema = AnnotationAdapter(tuple[int, str]).schema
    pipeline = ValidationPipeline(schema, (RuleBinding("label", (1,), Check()),))
    raw = '[12,"猫🙂"]'.encode()
    for split in range(len(raw) + 1):
        calls.clear()
        session = IncrementalOutputSession(pipeline)
        session.feed(raw[:split])
        progress = session.feed(raw[split:])
        assert progress.statistics.root_complete and not calls
        report = session.finish().report
        assert report.valid and report.output == [12, "猫🙂"]
        assert calls == [("猫🙂", (1,))]
    calls.clear()
    for raw in (b'[12,"ok",null]', b"[12]", b'["12","ok"]'):
        session = IncrementalOutputSession(pipeline)
        for byte in raw:
            session.feed(bytes((byte,)))
        assert not session.finish().report.valid
    assert not calls


@pytest.mark.parametrize(
    ("invalid", "expected"),
    [
        ("[7, false]", (GenerationFeedback("schema_type", "$[1]"),)),
        (
            '[7, "ok", {"private-key":"secret"}]',
            (GenerationFeedback("schema_max_length"), GenerationFeedback("schema_extra_item")),
        ),
    ],
)
def test_generation_reasks_retain_only_declared_positional_paths(invalid, expected):
    schema = AnnotationAdapter(tuple[int, str]).schema
    requests = []

    class Provider:
        async def generate(self, request):
            requests.append(request)
            return GeneratedResponse(
                invalid if len(requests) == 1 else '[7,"ok"]', TokenUsage(2, 3)
            )

    report = asyncio.run(AsyncGenerationRunner(ValidationPipeline(schema), Provider()).run("pair"))
    assert report.valid and report.output == [7, "ok"] and report.reported_tokens == 10
    assert requests[1].feedback == expected
    assert requests[0].schema == schema.json_schema()
    assert "private-key" not in repr(requests) and "secret" not in repr(report)


def test_factory_tuple_shape_is_checked_before_adapter_construction():
    calls = []

    @dataclass
    class Child:
        value: int

        def __post_init__(self):
            calls.append("child")

    def factory():
        calls.append("factory")
        return [2, "not-a-tuple"]

    @dataclass
    class Record:
        child: Child
        pair: tuple[int, str] = field(default_factory=factory)

    with pytest.raises(PayloadValidationError):
        DataclassAdapter(Record).validate_python({"child": {"value": 3}})
    assert calls == ["factory"]


def test_oversized_typed_tuple_rejects_before_projecting_any_member(monkeypatch):
    import payload_palette.dataclass_adapter as implementation

    calls = []
    original = implementation._scalar_value

    def observed(*args, **kwargs):
        calls.append("scalar")
        return original(*args, **kwargs)

    model = make_dataclass("Record", [("value", tuple[date, ...])])
    adapter = DataclassAdapter(model, limits=OutputLimits(max_nodes=3))
    monkeypatch.setattr(implementation, "_scalar_value", observed)
    with pytest.raises(OutputContractError, match="node"):
        adapter.dump_python(model((date(2024, 1, 1),) * 3))
    assert calls == []


def test_offline_example_repairs_then_constructs_and_serializes_typed_positions(capsys):
    from examples.positional_arrays import main

    main()
    assert json.loads(capsys.readouterr().out) == {
        "sample": [7, "camera"],
        "calibration": ["2024-02-29", "1.250"],
        "flags": [],
    }
