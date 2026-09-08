"""Independent public API contracts; live annotations deliberately avoid postponed evaluation."""

import json
from dataclasses import dataclass
from typing import Annotated, Any, ForwardRef, Literal, NotRequired, Required, TypedDict

import pytest
from examples.runtime_schema_adapter import run_example

from payload_palette import (
    AnnotationAdapter,
    FieldConstraints,
    JSONSerializationOptions,
    OutputContractError,
    OutputLimits,
    PayloadValidationError,
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    export_output_schema,
    load_output_schema,
    schema_for_annotation,
)


@pytest.mark.parametrize(
    ("annotation", "accepted", "rejected"),
    [
        (int, [0, -1, 2], [True, 1.0, "1", None]),
        (float, [0, 1.5, -2.0], [True, "1", None]),
        (bool, [True, False], [0, 1, "true"]),
        (str, ["", "hello", "😀"], [1, False, None]),
        (None, [None], [False, 0, "null"]),
        (type(None), [None], [False, 0]),
        (list[int], [[], [1, 2]], [[1.0], [True], (1,), {}]),
        (dict[str, int], [{}, {"any": 2}], [{"x": True}, {1: 1}, []]),
        (list[int | None], [[], [1, None]], [[True], [1.0], {}]),
        (Literal["a", "b"], ["a", "b"], ["c", 1]),
        (Literal[1, True, None, "x"], [1, True, None, "x"], [1.0, False, "1"]),
    ],
)
def test_strict_type_contracts_without_coercion(annotation, accepted, rejected):
    adapter = AnnotationAdapter(annotation)
    for value in accepted:
        actual = adapter.validate_python(value)
        assert actual == value
        assert type(actual) is type(value)
        assert adapter.validate_json_bytes(adapter.dump_json(value)) == value
    for value in rejected:
        with pytest.raises((PayloadValidationError, OutputContractError)):
            adapter.validate_python(value)


class ParentRecord(TypedDict):
    name: str


class ChildRecord(ParentRecord, total=False):
    count: Required[int]
    scores: list[float]
    comment: NotRequired[str | None]


def test_inherited_typeddict_required_optional_unknown_keys_and_isolation():
    adapter = AnnotationAdapter(ChildRecord)
    assert set(adapter.schema.required) == {"name", "count"}
    original = {"name": "sample", "count": 1, "scores": [0.4, 1]}
    accepted = adapter.validate_python(original)
    accepted["scores"].append(2)
    assert original["scores"] == [0.4, 1]
    for value in ({"name": "x"}, {"count": 1}, {"name": "x", "count": 1, "extra": 1}):
        with pytest.raises(PayloadValidationError):
            adapter.validate_python(value)
    assert adapter.validate_python({"name": "x", "count": 1, "comment": None})["comment"] is None


def test_typeddict_annotation_configuration_is_snapshotted():
    record = TypedDict("Record", {"value": int})  # noqa: UP013 -- runtime annotation fixture
    adapter = AnnotationAdapter(record)
    record.__annotations__["value"] = str
    record.__required_keys__ = frozenset()
    assert adapter.validate_python({"value": 1}) == {"value": 1}
    with pytest.raises(PayloadValidationError):
        adapter.validate_python({"value": "1"})


def test_constraints_apply_inside_nullable_branch_and_support_lengths():
    number = Annotated[float, FieldConstraints(minimum=0, maximum=1)] | None
    adapter = AnnotationAdapter(list[number])
    assert adapter.validate_python([None, 0, 1.0]) == [None, 0, 1.0]
    with pytest.raises(PayloadValidationError):
        adapter.validate_python([1.1])
    for annotation, accepted, rejected in (
        (Annotated[str, FieldConstraints(min_length=1, max_length=2)], "😀", ""),
        (Annotated[list[int], FieldConstraints(min_length=1, max_length=2)], [1], []),
    ):
        adapter = AnnotationAdapter(annotation)
        assert adapter.validate_python(accepted) == accepted
        with pytest.raises(PayloadValidationError):
            adapter.validate_python(rejected)


@dataclass
class UnsupportedDataclass:
    value: int


@pytest.mark.parametrize(
    "annotation",
    [
        Any,
        object,
        bytes,
        list,
        dict,
        dict[int, str],
        tuple,
        set[int],
        UnsupportedDataclass,
        "__import__('os').system('must not run')",
        ForwardRef("Unresolved"),
        Literal[1.5],
        Literal[b"a"],
        Annotated[int, "minimum=0"],
        Annotated[Annotated[int, FieldConstraints(minimum=0)], FieldConstraints(maximum=10)],
        Annotated[int | None, FieldConstraints(minimum=0)],
        Annotated[str, FieldConstraints(minimum=0)],
        Required[int],
        NotRequired[int],
    ],
)
def test_unsupported_annotations_fail_explicitly_without_evaluation(annotation):
    with pytest.raises(SchemaDefinitionError):
        schema_for_annotation(annotation)


def test_unresolved_typeddict_fields_are_not_evaluated():
    record = TypedDict("Record", {"field": "__import__('os')"})  # noqa: UP013
    with pytest.raises(SchemaDefinitionError, match="annotation_unresolved"):
        schema_for_annotation(record)


@pytest.mark.parametrize("metadata", [None, [], {1: int}, {"x" * 257: int}, {"\ud800": int}])
def test_malformed_typeddict_annotations_rejected(metadata):
    record = TypedDict("Record", {"field": int})  # noqa: UP013
    record.__annotations__ = metadata
    with pytest.raises(SchemaDefinitionError, match="annotation_typeddict"):
        schema_for_annotation(record)


@pytest.mark.parametrize("metadata", [None, [], frozenset({"missing"}), frozenset({1})])
def test_malformed_typeddict_required_metadata_rejected(metadata):
    record = TypedDict("Record", {"field": int})  # noqa: UP013
    record.__required_keys__ = metadata
    with pytest.raises(SchemaDefinitionError, match="annotation_typeddict"):
        schema_for_annotation(record)


def test_recursive_annotation_and_compiler_budgets():
    recursive = TypedDict("Recursive", {})  # noqa: UP013
    recursive.__annotations__["self"] = recursive
    with pytest.raises(SchemaDefinitionError, match="annotation_cycle"):
        schema_for_annotation(recursive)
    for annotation, limits in (
        (list[int], SchemaDefinitionLimits(max_depth=0)),
        (list[int], SchemaDefinitionLimits(max_nodes=1)),
        (int | str | None, SchemaDefinitionLimits(max_branches=2)),
        (Literal[1, "a", None], SchemaDefinitionLimits(max_branches=2)),
        (Literal[1, "a"], SchemaDefinitionLimits(max_nodes=2)),
        (Literal["long"], SchemaDefinitionLimits(max_characters=2)),
        (dict[str, list[int]], SchemaDefinitionLimits(max_depth=1)),
    ):
        with pytest.raises(SchemaDefinitionError):
            schema_for_annotation(annotation, limits=limits)
    assert (
        schema_for_annotation(list[int], limits=SchemaDefinitionLimits(max_nodes=2)).kind == "array"
    )
    assert schema_for_annotation(dict[str, int]).additional_properties.kind == "integer"


@pytest.mark.parametrize(
    "options",
    [
        {"minimum": True},
        {"maximum": float("inf")},
        {"minimum": 10**1000},
        {"minimum": 2, "maximum": 1},
        {"min_length": True},
        {"max_length": -1},
        {"min_length": 2, "max_length": 1},
        {"max_length": 8_000_001},
    ],
)
def test_field_constraints_configuration_is_strict(options):
    with pytest.raises(ValueError):
        FieldConstraints(**options)


@pytest.mark.parametrize(
    "options",
    [
        {"ensure_ascii": 1},
        {"sort_keys": 0},
        {"indent": True},
        {"indent": 9},
        {"indent": -1},
        {"max_output_bytes": 0},
        {"max_output_bytes": True},
        {"max_output_bytes": 32_000_001},
    ],
)
def test_serialization_options_configuration_is_strict(options):
    with pytest.raises(ValueError):
        JSONSerializationOptions(**options)


def test_serialization_policies_do_not_coerce_drop_or_mutate_values():
    value = {"z": "é", "a": "汉😀"}
    assert AnnotationAdapter(dict[str, str]).dump_json(value) == '{"a":"汉😀","z":"é"}'.encode()
    options = JSONSerializationOptions(ensure_ascii=True, sort_keys=False, indent=2)
    actual = AnnotationAdapter(dict[str, str], serialization=options).dump_json(value)
    assert actual == json.dumps(value, ensure_ascii=True, sort_keys=False, indent=2).encode()
    assert value == {"z": "é", "a": "汉😀"}
    assert json.loads(actual) == value
    assert AnnotationAdapter(float).dump_json(1) == b"1"
    assert AnnotationAdapter(float).dump_json(1.0) == b"1.0"


@pytest.mark.parametrize("text", ["ascii", "é", "汉", "😀", 'a"b'])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_serializer_measures_exact_encoded_byte_limit(text, ascii_only):
    expected = json.dumps(text, ensure_ascii=ascii_only).encode()
    exact = JSONSerializationOptions(ensure_ascii=ascii_only, max_output_bytes=len(expected))
    assert AnnotationAdapter(str, serialization=exact).dump_json(text) == expected
    small = JSONSerializationOptions(ensure_ascii=ascii_only, max_output_bytes=len(expected) - 1)
    with pytest.raises(OutputContractError, match="byte limit"):
        AnnotationAdapter(str, serialization=small).dump_json(text)


def test_adapter_rejects_malformed_input_and_enforces_independent_resource_limits():
    adapter = AnnotationAdapter(dict[str, int])
    with pytest.raises(PayloadValidationError, match="duplicate_json_key"):
        adapter.validate_json_bytes(b'{"a":1,"a":2}')
    with pytest.raises(PayloadValidationError, match="input_too_large"):
        adapter.validate_json_bytes(b'{"a":1}', max_input_bytes=2)
    with pytest.raises(PayloadValidationError):
        adapter.dump_json({"a": 1.0})
    with pytest.raises(OutputContractError, match="node"):
        AnnotationAdapter(list[int], limits=OutputLimits(max_nodes=2)).validate_python([1, 2])
    cyclic = []
    cyclic.append(cyclic)
    with pytest.raises(OutputContractError, match="cyclic"):
        AnnotationAdapter(list[int]).validate_python(cyclic)
    for field in ("limits", "definition_limits", "serialization"):
        with pytest.raises(ValueError):
            AnnotationAdapter(int, **{field: None})
    with pytest.raises(ValueError):
        schema_for_annotation(int, limits={})


def test_python_strict_integer_and_json_integer_interchange_are_explicit():
    adapter = AnnotationAdapter(int)
    mathematical = load_output_schema({"type": "integer", "enum": [1.0]})
    assert mathematical.validate(1) == mathematical.validate(1.0) == ()
    assert mathematical.validate(True) and mathematical.validate(1.5)
    assert load_output_schema({"type": "integer", "const": 1}).validate(1.0) == ()
    assert load_output_schema(export_output_schema(mathematical)).validate(1.0) == ()
    strict_export = export_output_schema(adapter.schema)
    assert strict_export["x-payload-strict-integer"] is True
    assert load_output_schema(strict_export).validate(1.0)
    assert "x-payload-strict-integer" not in adapter.schema.json_schema()
    assert load_output_schema(adapter.schema.json_schema()).validate(1.0) == ()
    with pytest.raises(PayloadValidationError):
        adapter.validate_python(1.0)


def test_runtime_schema_example_executes_end_to_end():
    result = run_example()
    assert result["output"] == {"label": "bicycle", "confidence": None, "tags": ["vision"]}
    assert json.loads(result["serialized"]) == result["output"]
    assert result["schema"]["additionalProperties"] is False
