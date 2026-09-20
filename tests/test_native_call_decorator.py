"""Explicitly trusted annotation-derived native calls, without coercion."""

import asyncio
import inspect
import sys
from decimal import Decimal
from functools import partial
from typing import Any, ForwardRef

import pytest

from payload_palette import (
    PayloadValidationError,
    SchemaDefinitionError,
    native_validated_call,
)


def test_decorator_binds_all_native_parameter_kinds_without_conversion() -> None:
    entries: list[tuple[object, ...]] = []

    @native_validated_call(trust_annotations=True, validate_return=True)
    def target(
        amount: Decimal, /, count: int = 1, *extras: int, label: str, **options: int
    ) -> Decimal:
        entries.append((amount, count, extras, label, options))
        return amount

    amount = Decimal("12.3400")
    assert target(amount, 2, 3, label="x", option=4) is amount
    assert entries == [(amount, 2, (3,), "x", {"option": 4})]
    assert entries[0][0] is amount
    with pytest.raises(PayloadValidationError):
        target(amount, "2", label="x")
    with pytest.raises(PayloadValidationError):
        target(amount)
    assert len(entries) == 1
    assert target.__name__ == "target"
    assert inspect.signature(target).parameters["amount"].kind is inspect.Parameter.POSITIONAL_ONLY


def test_return_failure_follows_one_irreversible_body_entry() -> None:
    entries = []

    @native_validated_call(trust_annotations=True, validate_return=True)
    def target(value: int) -> int:
        entries.append(value)
        return "invalid"  # type: ignore[return-value]

    with pytest.raises(PayloadValidationError):
        target(1)
    assert entries == [1]


def test_declared_map_is_snapshotted_and_target_exception_is_not_wrapped() -> None:
    failure = RuntimeError("target failed")
    entries = []

    def target(value: int) -> int:
        entries.append(value)
        raise failure

    checked = native_validated_call(target, trust_annotations=True, validate_return=True)
    target.__annotations__["value"] = str
    with pytest.raises(RuntimeError) as raised:
        checked(3)
    assert raised.value is failure
    with pytest.raises(PayloadValidationError):
        checked("3")
    assert entries == [3]


def test_return_check_requires_declared_return_annotation() -> None:
    def target(value: int):
        return value

    with pytest.raises(SchemaDefinitionError):
        native_validated_call(target, trust_annotations=True, validate_return=True)


def test_async_wrapper_remains_lazy_direct_coroutine() -> None:
    async def scenario() -> None:
        entries = []
        owner = asyncio.current_task()

        @native_validated_call(trust_annotations=True, validate_return=True)
        async def target(value: list[int]) -> list[int]:
            entries.append(asyncio.current_task())
            await asyncio.sleep(0)
            return value

        assert inspect.iscoroutinefunction(target)
        value = [1]
        pending = target(value)
        assert entries == []
        assert await pending is value
        assert entries == [owner]
        with pytest.raises(PayloadValidationError):
            await target([True])
        assert entries == [owner]

    asyncio.run(scenario())


@pytest.mark.parametrize("bad_annotation", [Any, ForwardRef("Missing"), "int"])
def test_unsupported_declared_annotations_fail_before_body(bad_annotation: object) -> None:
    entries = []

    def target(value: int) -> int:
        entries.append(value)
        return value

    target.__annotations__["value"] = bad_annotation
    with pytest.raises(SchemaDefinitionError):
        native_validated_call(target, trust_annotations=True)
    assert entries == []


def test_future_string_annotations_are_not_evaluated_or_resolved() -> None:
    entries: list[int] = []
    namespace: dict[str, object] = {"entries": entries}
    exec(
        compile(
            "from __future__ import annotations\n"
            "def target(value: int) -> int:\n    entries.append(value)\n    return value\n",
            "<future-string-annotation-test>",
            "exec",
            dont_inherit=True,
        ),
        namespace,
    )
    with pytest.raises(SchemaDefinitionError):
        native_validated_call(namespace["target"], trust_annotations=True)
    assert entries == []


def test_missing_annotation_and_unsupported_callable_fail_closed() -> None:
    def target(value):
        return value

    with pytest.raises(SchemaDefinitionError):
        native_validated_call(target, trust_annotations=True)
    with pytest.raises(SchemaDefinitionError):
        native_validated_call(partial(target, 1), trust_annotations=True)

    class CallableObject:
        def __call__(self, value: int) -> int:
            return value

    with pytest.raises(SchemaDefinitionError):
        native_validated_call(CallableObject(), trust_annotations=True)


def test_permission_is_explicit_and_rejects_false_without_reading_annotations() -> None:
    def target(value: int) -> int:
        return value

    with pytest.raises((TypeError, ValueError)):
        native_validated_call(target)
    with pytest.raises(ValueError, match="trust_annotations"):
        native_validated_call(target, trust_annotations=False)


@pytest.mark.skipif(sys.version_info < (3, 14), reason="requires deferred CPython annotations")
def test_deferred_annotation_runs_once_only_after_permission() -> None:
    entries: list[str] = []

    def marker() -> type[int]:
        entries.append("annotation")
        return int

    namespace: dict[str, object] = {"marker": marker}
    source = compile(
        "def target(value: marker()) -> int:\n    return value\n",
        "<trusted-native-annotation-test>",
        "exec",
        dont_inherit=True,
    )
    exec(source, namespace)
    target = namespace["target"]
    target.__dict__["source_only_metadata"] = "do not copy"
    target.__dict__["__annotate__"] = lambda *_args: pytest.fail("copied annotation hook")
    assert not entries
    with pytest.raises(ValueError, match="trust_annotations"):
        native_validated_call(target, trust_annotations=False)
    assert not entries
    checked = native_validated_call(target, trust_annotations=True)
    assert entries == ["annotation"]
    assert checked(3) == 3
    assert checked.__annotations__ == {}
    assert "source_only_metadata" not in checked.__dict__
    assert "__annotate__" not in checked.__dict__
    assert entries == ["annotation"]


@pytest.mark.skipif(sys.version_info < (3, 14), reason="requires deferred CPython annotations")
@pytest.mark.parametrize("bad_option", ["limits", "definition_limits", "call_limits"])
def test_invalid_options_do_not_evaluate_deferred_annotations(bad_option: str) -> None:
    entries: list[str] = []

    def marker() -> type[int]:
        entries.append("annotation")
        return int

    namespace: dict[str, object] = {"marker": marker}
    exec(
        compile(
            "def target(value: marker()) -> int:\n    return value\n",
            "<invalid-options-annotation-test>",
            "exec",
            dont_inherit=True,
        ),
        namespace,
    )
    with pytest.raises(ValueError, match="options/limits"):
        native_validated_call(namespace["target"], trust_annotations=True, **{bad_option: object()})
    assert entries == []


@pytest.mark.skipif(sys.version_info < (3, 14), reason="requires deferred CPython annotations")
@pytest.mark.parametrize(
    "source",
    [
        "def target(value: marker()):\n    yield value\n",
        "async def target(value: marker()):\n    yield value\n",
    ],
)
def test_unsupported_generator_rejected_before_deferred_annotation(source: str) -> None:
    entries: list[str] = []

    def marker() -> type[int]:
        entries.append("annotation")
        return int

    namespace: dict[str, object] = {"marker": marker}
    exec(
        compile(source, "<unsupported-generator-annotation-test>", "exec", dont_inherit=True),
        namespace,
    )
    with pytest.raises(SchemaDefinitionError):
        native_validated_call(namespace["target"], trust_annotations=True)
    assert entries == []
