import inspect
import sys
import types
from dataclasses import FrozenInstanceError, dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from ipaddress import (
    IPv4Address,
    IPv4Interface,
    IPv4Network,
    IPv6Address,
    IPv6Interface,
    IPv6Network,
)
from typing import Annotated, Any, ForwardRef, Literal, NotRequired, TypedDict
from uuid import UUID

import pytest

import payload_palette as api
from payload_palette import (
    CallAdapter,
    CallLimits,
    FieldConstraints,
    OutputContractError,
    PayloadValidationError,
    SchemaDefinitionError,
)


def identity(value):
    return value


def test_explicit_native_call_api_exists():
    adapter = CallAdapter(identity, annotations={"value": int, "return": int}, validate_return=True)
    assert adapter.call(3) == 3
    assert CallLimits().max_parameters == 64
    assert {"CallAdapter", "CallLimits"} <= set(api.__all__)
    with pytest.raises(FrozenInstanceError):
        adapter.validate_return = False


@pytest.mark.parametrize(
    ("annotation", "values", "bad"),
    [
        (int, [0, -3, 1 << 849], [True, 1.0, "1", None]),
        (float, [1, -1.5], [True, "1", None]),
        (bool, [True, False], [1, 0, "true"]),
        (str, ["", "文😀"], [1, b"a", None]),
        (None, [None], [0, "null"]),
        (list[int], [[], [1, 2]], [(1, 2), [True], [1.0]]),
        (dict[str, int], [{}, {"x": 1}], [{1: 1}, {"x": True}, []]),
        (tuple[int, ...], [(), (1, 2)], [[1, 2], (True,)]),
        (tuple[int, str], [(1, "x")], [(1,), (1, 2), [1, "x"]]),
        (tuple[()], [()], [(1,), []]),
        (Literal[1, True, "a", None], [1, True, "a", None], [1.0, False, "b"]),
        (int | None, [1, None], [True, 1.0]),
        (int | float, [1, 1.5], [True, "1"]),
        (Annotated[int, FieldConstraints(minimum=1, maximum=3)], [1, 3], [0, 4]),
    ],
)
def test_native_values_are_checked_without_conversion(annotation, values, bad):
    adapter = CallAdapter(
        identity, annotations={"value": annotation, "return": annotation}, validate_return=True
    )
    for value in values:
        assert adapter.call(value) is value
    for value in bad:
        with pytest.raises((PayloadValidationError, OutputContractError)):
            adapter.call(value)


@pytest.mark.parametrize(
    ("annotation", "value", "wire"),
    [
        (date, date(2024, 2, 29), "2024-02-29"),
        (datetime, datetime(2024, 2, 29, tzinfo=UTC), "2024-02-29T00:00:00Z"),
        (time, time(3, 4, 5), "03:04:05"),
        (timedelta, timedelta(microseconds=-1), "-PT0.000001S"),
        (Decimal, Decimal("-0.00"), "-0.00"),
        (
            UUID,
            UUID("12345678-1234-5678-1234-567812345678"),
            "12345678-1234-5678-1234-567812345678",
        ),
        (IPv4Address, IPv4Address("192.0.2.1"), "192.0.2.1"),
        (IPv6Address, IPv6Address("2001:db8::1"), "2001:db8::1"),
        (IPv4Network, IPv4Network("192.0.2.0/24"), "192.0.2.0/24"),
        (IPv6Network, IPv6Network("2001:db8::/32"), "2001:db8::/32"),
        (IPv4Interface, IPv4Interface("192.0.2.1/24"), "192.0.2.1/24"),
        (IPv6Interface, IPv6Interface("2001:db8::1/32"), "2001:db8::1/32"),
    ],
)
def test_all_twelve_scalar_objects_retain_identity_and_reject_wire(annotation, value, wire):
    adapter = CallAdapter(
        identity, annotations={"value": annotation, "return": annotation}, validate_return=True
    )
    assert adapter.call(value) is value
    with pytest.raises(PayloadValidationError):
        adapter.call(wire)


def test_dataclass_identity_fields_defaults_and_aliasing_without_construction():
    callbacks = []

    @dataclass
    class Box:
        values: list[int] = field(default_factory=lambda: callbacks.append("factory") or [])

        def __post_init__(self):
            callbacks.append("constructor")

    box = Box([1])
    callbacks.clear()

    def target(left, right, /):
        assert left is right is box
        left.values.append(2)
        return left

    adapter = CallAdapter(
        target, annotations={"left": Box, "right": Box, "return": Box}, validate_return=True
    )
    assert adapter.call(box, box) is box
    assert box.values == [1, 2] and callbacks == []
    with pytest.raises(PayloadValidationError):
        adapter.call({"values": [1]}, box)
    box.values.append("bad")
    with pytest.raises(PayloadValidationError):
        adapter.call(box, box)
    assert callbacks == []
    del box.values
    with pytest.raises(PayloadValidationError):
        adapter.call(box, box)


def test_materialized_typeddict_optional_keys_and_map_snapshot():
    class Record(TypedDict):
        required: int
        note: NotRequired[str]

    record = Record
    record.__annotations__ = {"required": int, "note": NotRequired[str]}
    annotations = {"value": record}
    adapter = CallAdapter(identity, annotations=annotations)
    annotations["value"] = str
    record.__annotations__ = {"required": str}
    value = {"required": 3}
    assert adapter.call(value) is value
    with pytest.raises(PayloadValidationError):
        adapter.call({"note": "missing"})
    with pytest.raises(PayloadValidationError):
        adapter.call({"required": True})


def test_constructing_union_roundtrip_ambiguity_is_preserved():
    with pytest.raises(PayloadValidationError, match="ambiguous"):
        CallAdapter(identity, annotations={"value": date | str}).call(date(2024, 1, 1))
    value = date(2024, 1, 1)
    assert CallAdapter(identity, annotations={"value": date | UUID}).call(value) is value


def test_mutable_defaults_are_captured_by_identity_and_rechecked():
    default = [1]

    def target(value=default, *, suffix=2):
        return value, suffix

    adapter = CallAdapter(target, annotations={"value": list[int], "suffix": int})
    target.__defaults__ = ([99],)
    target.__kwdefaults__ = {"suffix": 99}
    result = adapter.call()
    assert result[0] is default and result[1] == 2
    default.append(3)
    assert adapter.call()[0] is default
    default.append("bad")
    with pytest.raises(PayloadValidationError):
        adapter.call()
    assert adapter.call([7]) == ([7], 2)


def test_invalid_default_is_rejected_before_target_even_if_call_could_override():
    entries = []

    def target(value="bad"):
        entries.append(value)

    with pytest.raises(PayloadValidationError):
        CallAdapter(target, annotations={"value": int})
    assert entries == []


@pytest.mark.parametrize(
    "annotations",
    [
        None,
        [],
        {},
        {"other": int},
        {1: int},
        {"value": "int"},
        {"value": ForwardRef("int")},
        {"value": Any},
        {"value": set[int]},
        {"value": int, "return": "int"},
    ],
)
def test_bad_annotation_maps_are_rejected(annotations):
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(identity, annotations=annotations)


def test_return_annotation_is_explicit_and_return_bypass_is_real():
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(identity, annotations={"value": int}, validate_return=True)
    raw = object()

    def target():
        return raw

    assert CallAdapter(target, annotations={}).call() is raw
    assert CallAdapter(target, annotations={"return": int}).call() is raw
    with pytest.raises(PayloadValidationError):
        CallAdapter(target, annotations={"return": int}, validate_return=True).call()


def test_invalid_return_does_not_rollback_target_side_effects():
    values = []

    def target(value):
        value.append("side effect")
        return "invalid"

    adapter = CallAdapter(
        target, annotations={"value": list[str], "return": int}, validate_return=True
    )
    with pytest.raises(PayloadValidationError) as error:
        adapter.call(values)
    assert values == ["side effect"]
    assert error.value.issues[0].path == '$["return"]'


@pytest.mark.parametrize(
    "error",
    [
        TypeError("body"),
        ValueError("body"),
        MemoryError("body"),
        KeyboardInterrupt(),
        SystemExit(7),
    ],
)
def test_target_exceptions_propagate_unchanged(error):
    def target():
        raise error

    with pytest.raises(type(error)) as caught:
        CallAdapter(target, annotations={}).call()
    assert caught.value is error


def test_target_annotation_and_forged_signature_are_never_read_or_unwrapped():
    events = []

    def annotation():
        events.append("annotation")
        return str

    def target(value: annotation() = 1):
        return value

    before = list(events)
    target.__signature__ = object()
    target.__wrapped__ = target
    adapter = CallAdapter(target, annotations={"value": int})
    assert adapter.call() == 1
    assert adapter.call(3) == 3
    assert events == before
    del target.__signature__
    del target.__wrapped__
    inspect.signature(target, eval_str=False, follow_wrapped=False)
    assert events == before + (["annotation"] if sys.version_info >= (3, 14) else [])


def test_native_dataclass_plan_skips_constructor_annotation_and_signature_setup():
    events = []

    def annotation():
        events.append("constructor annotation")
        return int

    @dataclass
    class Box:
        """Explicit documentation avoids dataclasses' generated signature docstring."""

        value: int

        def __init__(self, value: annotation(), /):
            self.value = value

    box = Box(3)
    before = list(events)

    def target(value=box):
        return value

    adapter = CallAdapter(target, annotations={"value": Box, "return": Box}, validate_return=True)
    assert adapter.call() is box and events == before
    # The existing constructor adapter still refuses a positional-only constructor.
    with pytest.raises(SchemaDefinitionError):
        api.DataclassAdapter(Box)


def test_deferred_typeddict_annotations_are_not_materialized():
    events = []

    class Record(TypedDict):
        value: int

    record = Record
    if sys.version_info >= (3, 14):

        def annotations(format):
            events.append(format)
            return {"value": int}

        record.__annotate__ = annotations
        with pytest.raises(SchemaDefinitionError, match="materialized"):
            CallAdapter(identity, annotations={"value": record})
        assert events == []
    record.__annotations__ = {"value": int}
    assert CallAdapter(identity, annotations={"value": record}).call({"value": 2}) == {"value": 2}
    assert events == []


def test_code_replacement_is_rejected_before_call():
    def first(value):
        return value

    def replacement(value):
        raise AssertionError("must not run")

    adapter = CallAdapter(first, annotations={"value": int})
    first.__code__ = replacement.__code__
    with pytest.raises(OutputContractError, match="code changed"):
        adapter.call(1)


def test_exact_bound_method_receiver_and_resolved_static_class_methods():
    class Owner:
        def method(self, value=1):
            return self, value

        @classmethod
        def class_method(cls, value=2):
            return cls, value

        @staticmethod
        def static_method(value=3):
            return value

    owner = Owner()
    assert CallAdapter(owner.method, annotations={"value": int}).call() == (owner, 1)
    assert CallAdapter(Owner.class_method, annotations={"value": int}).call() == (Owner, 2)
    assert CallAdapter(Owner.static_method, annotations={"value": int}).call() == 3
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(owner.method, annotations={"self": int, "value": int})
    with pytest.raises(SchemaDefinitionError):
        CallAdapter(Owner.method, annotations={"value": int})


def test_unsupported_targets_never_run():
    class CallableObject:
        def __call__(self):
            raise AssertionError("must not run")

    def generator():
        yield 1

    async def async_generator():
        yield 1

    def receiver_varargs(*values):
        return values

    for target in [
        CallableObject,
        CallableObject(),
        len,
        partial(identity, 1),
        generator,
        async_generator,
        types.MethodType(receiver_varargs, object()),
        staticmethod(identity),
        classmethod(identity),
        property(identity),
    ]:
        with pytest.raises(SchemaDefinitionError):
            CallAdapter(target, annotations={})


def test_reserved_looking_keywords_and_sanitized_binding_error():
    def target(**extras):
        return extras

    adapter = CallAdapter(target, annotations={"extras": int})
    value = {"self": 1, "function": 2, "annotations": 3, "limits": 4, "not-an-identifier": 5}
    assert adapter.call(**value) == value
    with pytest.raises(PayloadValidationError) as error:
        CallAdapter(identity, annotations={"value": str}).call(value="secret", extra="hidden")
    assert error.value.to_dict() == {
        "valid": False,
        "error_count": 1,
        "errors": [
            {
                "code": "call_bind",
                "message": "arguments do not match the captured signature",
                "path": '$["arguments"]',
            }
        ],
    }


def test_offline_example_has_handwritten_complete_result():
    from examples.native_call_validation import run_example

    assert run_example() == {
        "day": "2026-09-12",
        "amount": "123.4500",
        "entries": ["sync", "rejected-input", "async", "side-effect", "rejected-return"],
        "identity_preserved": True,
        "default_labels": ["sample"],
    }
