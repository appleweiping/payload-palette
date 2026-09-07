from __future__ import annotations

from typing import Any

import pytest

from payload_palette import OutputContractError, OutputLimits, OutputSchema


def test_nested_schema_reports_specific_independent_paths() -> None:
    schema = OutputSchema(
        "object",
        properties={
            "name": OutputSchema("string", min_length=1),
            "scores": OutputSchema("array", items=OutputSchema("number", minimum=0, maximum=1)),
            "flag": OutputSchema("boolean"),
        },
        required=("name",),
    )
    issues = schema.validate({"scores": [True, -1, 2], "unexpected": 5})
    assert [(issue.code, issue.path) for issue in issues] == [
        ("schema_required", '$["name"]'),
        ("schema_type", '$["scores"][0]'),
        ("schema_minimum", '$["scores"][1]'),
        ("schema_maximum", '$["scores"][2]'),
        ("schema_extra", '$["unexpected"]'),
    ]
    assert not schema.validate({"name": "test", "scores": [0, 0.5, 1], "flag": False})


@pytest.mark.parametrize(
    ("schema", "value", "code"),
    [
        (OutputSchema("null"), 0, "schema_type"),
        (OutputSchema("boolean"), 1, "schema_type"),
        (OutputSchema("integer"), True, "schema_type"),
        (OutputSchema("integer"), 1.0, "schema_type"),
        (OutputSchema("number"), "1", "schema_type"),
        (OutputSchema("string", min_length=1), "", "schema_min_length"),
        (OutputSchema("string", max_length=2), "abc", "schema_max_length"),
        (OutputSchema("integer", enum=(1, 2)), 3, "schema_enum"),
        (OutputSchema("string", enum=("a", "b")), "A", "schema_enum"),
        (OutputSchema("array", items=OutputSchema("null"), min_length=1), [], "schema_min_length"),
        (
            OutputSchema("array", items=OutputSchema("null"), max_length=0),
            [None],
            "schema_max_length",
        ),
    ],
)
def test_strict_type_and_value_constraints(schema: OutputSchema, value: Any, code: str) -> None:
    assert schema.validate(value)[0].code == code


def test_json_schema_export_and_configuration_snapshots() -> None:
    properties = {"n": OutputSchema("integer", minimum=0, maximum=3, enum=(1, 2))}
    schema = OutputSchema("object", properties=properties, required=("n",))
    properties.clear()
    exported = schema.json_schema()
    assert exported == {
        "type": "object",
        "properties": {"n": {"type": "integer", "minimum": 0, "maximum": 3, "enum": [1, 2]}},
        "required": ["n"],
        "additionalProperties": False,
    }
    exported.clear()
    assert not schema.validate({"n": 2})
    with pytest.raises(TypeError):
        schema.properties["x"] = OutputSchema("null")  # type: ignore[index]
    assert OutputSchema(
        "array", items=OutputSchema("string"), min_length=1, max_length=2
    ).json_schema() == {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2}
    assert OutputSchema("string", min_length=1, max_length=2).json_schema() == {
        "type": "string",
        "minLength": 1,
        "maxLength": 2,
    }
    assert not OutputSchema("object", additional_properties=True).validate({"anything": None})


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": "unknown"},
        {"kind": "object", "properties": []},
        {"kind": "object", "properties": {str(n): OutputSchema("null") for n in range(257)}},
        {"kind": "object", "properties": {"x": object()}},
        {"kind": "object", "properties": {"\ud800": OutputSchema("null")}},
        {"kind": "object", "required": ("absent",)},
        {"kind": "object", "required": []},
        {"kind": "object", "additional_properties": 1},
        {"kind": "string", "additional_properties": True},
        {"kind": "array"},
        {"kind": "string", "items": OutputSchema("null")},
        {"kind": "string", "minimum": 0},
        {"kind": "number", "minimum": True},
        {"kind": "number", "maximum": float("inf")},
        {"kind": "number", "minimum": 2, "maximum": 1},
        {"kind": "string", "min_length": -1},
        {"kind": "integer", "max_length": 2},
        {"kind": "string", "min_length": 3, "max_length": 2},
        {"kind": "string", "enum": []},
        {"kind": "string", "enum": ()},
        {"kind": "string", "enum": (None,)},
        {"kind": "string", "enum": ("x" * 2049,)},
        {"kind": "array", "items": OutputSchema("null"), "enum": (None,)},
    ],
)
def test_invalid_schema_configuration(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        OutputSchema(**kwargs)


def test_schema_expansion_and_depth_are_bounded() -> None:
    child = OutputSchema("null")
    for _ in range(32):
        child = OutputSchema("array", items=child)
    with pytest.raises(ValueError, match="depth"):
        OutputSchema("array", items=child)
    child = OutputSchema("null")
    for _ in range(3):
        child = OutputSchema("object", properties={str(n): child for n in range(10)})
    with pytest.raises(ValueError, match="4096"):
        OutputSchema("object", properties={str(n): child for n in range(10)})
    child = OutputSchema("string", enum=tuple(str(n) + "x" * 2000 for n in range(256)))
    with pytest.raises(ValueError, match="character"):
        OutputSchema("object", properties={"a": child, "b": child})


@pytest.mark.parametrize(
    "value", [float("nan"), float("inf"), 2**900, {1: None}, "\ud800", (1,), object()]
)
def test_non_json_values_rejected_before_validation(value: Any) -> None:
    with pytest.raises(OutputContractError):
        OutputSchema("null").validate(value)


def test_snapshot_bounds_cycles_and_shared_children() -> None:
    cyclic: list[Any] = []
    cyclic.append(cyclic)
    with pytest.raises(OutputContractError, match="cyclic"):
        OutputSchema("null").validate(cyclic)
    shared = [None]
    schema = OutputSchema("array", items=OutputSchema("array", items=OutputSchema("null")))
    assert not schema.validate([shared, shared])
    with pytest.raises(OutputContractError, match="node"):
        schema.validate([shared, shared], limits=OutputLimits(max_nodes=3))
    with pytest.raises(OutputContractError, match="depth"):
        schema.validate([shared], limits=OutputLimits(max_depth=1))
    with pytest.raises(OutputContractError, match="character"):
        OutputSchema("object", additional_properties=True).validate(
            {"ab": "cd"}, limits=OutputLimits(max_characters=3)
        )
    with pytest.raises(OutputContractError, match="node"):
        OutputSchema("object", additional_properties=True).validate(
            {"a": 1, "b": 2}, limits=OutputLimits(max_nodes=2)
        )
    with pytest.raises(OutputContractError, match="Unicode"):
        OutputSchema("object", additional_properties=True).validate({"\ud800": None})
    with pytest.raises(OutputContractError, match="character"):
        OutputSchema("object", additional_properties=True).validate(
            {"too-long": None}, limits=OutputLimits(max_characters=2)
        )


def test_number_enum_equivalence_does_not_confuse_boolean_with_integer() -> None:
    assert not OutputSchema("number", enum=(1,)).validate(1.0)
    assert not OutputSchema("number", enum=(1.0,)).validate(1)
    assert OutputSchema("integer", enum=(1,)).validate(True)[0].code == "schema_type"


def test_issue_aggregation_is_explicitly_bounded() -> None:
    schema = OutputSchema("array", items=OutputSchema("null"))
    issues = schema.validate([1, 2, 3, 4], limits=OutputLimits(max_issues=1))
    assert [issue.code for issue in issues] == ["schema_type", "schema_issue_limit"]


@pytest.mark.parametrize(
    "field", ["max_depth", "max_nodes", "max_characters", "max_issues", "max_invocations"]
)
@pytest.mark.parametrize("value", [True, 0, -1, 1.1, 10**20])
def test_limit_configuration_has_hard_ceilings(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        OutputLimits(**{field: value})


def test_invalid_limit_type_is_rejected() -> None:
    with pytest.raises(ValueError):
        OutputSchema("null").validate(None, limits={})  # type: ignore[arg-type]
