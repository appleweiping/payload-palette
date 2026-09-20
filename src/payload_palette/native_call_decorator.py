"""Explicitly trusted declaration-derived native calls over CallAdapter."""

from __future__ import annotations

import inspect
import sys
from collections.abc import Callable
from types import FunctionType
from typing import ParamSpec, TypeVar, cast, overload

from payload_palette.call_validation import CallAdapter, CallLimits
from payload_palette.output_schema import OutputLimits
from payload_palette.schema_io import SchemaDefinitionLimits, _definition_error

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _declared_annotations(function: FunctionType) -> dict[str, object]:
    # Both access paths may execute trusted annotation code on Python 3.14.
    # The public permission check must happen before reaching this function.
    if sys.version_info >= (3, 14):
        import annotationlib

        annotations = annotationlib.get_annotations(
            function, format=annotationlib.Format.VALUE, eval_str=False
        )
    else:
        annotations = inspect.get_annotations(function, eval_str=False)
    if type(annotations) is not dict:
        raise _definition_error("call_annotations", "annotation map must be an exact dict", ())
    return cast(dict[str, object], dict(annotations))


@overload
def native_validated_call(
    function: Callable[_P, _R],
    /,
    *,
    trust_annotations: bool,
    validate_return: bool = False,
    limits: OutputLimits | None = None,
    definition_limits: SchemaDefinitionLimits | None = None,
    call_limits: CallLimits | None = None,
) -> Callable[_P, _R]: ...


@overload
def native_validated_call(
    function: None = None,
    /,
    *,
    trust_annotations: bool,
    validate_return: bool = False,
    limits: OutputLimits | None = None,
    definition_limits: SchemaDefinitionLimits | None = None,
    call_limits: CallLimits | None = None,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]: ...


def native_validated_call(
    function: Callable[..., object] | None = None,
    /,
    *,
    trust_annotations: bool,
    validate_return: bool = False,
    limits: OutputLimits | None = None,
    definition_limits: SchemaDefinitionLimits | None = None,
    call_limits: CallLimits | None = None,
) -> Callable[..., object]:
    """Compile one strict call plan from explicitly trusted source annotations.

    This opts into annotation evaluation, which can run arbitrary user code on
    Python 3.14. Values are not coerced or copied; CallAdapter owns all actual
    binding, validation, invocation and resource semantics.
    """
    if type(trust_annotations) is not bool or not trust_annotations:
        raise ValueError("trust_annotations=True is required before annotation access")
    resolved_limits = OutputLimits() if limits is None else limits
    resolved_definition_limits = (
        SchemaDefinitionLimits() if definition_limits is None else definition_limits
    )
    resolved_call_limits = CallLimits() if call_limits is None else call_limits
    # Reject malformed options before Python 3.14 can evaluate a deferred annotation.
    if (
        type(validate_return) is not bool
        or type(resolved_limits) is not OutputLimits
        or type(resolved_definition_limits) is not SchemaDefinitionLimits
        or type(resolved_call_limits) is not CallLimits
    ):
        raise ValueError("call options/limits must have their declared types")

    def decorate(target: Callable[..., object]) -> Callable[..., object]:
        if type(target) is not FunctionType:
            raise _definition_error("call_function", "requires an exact native Python function", ())
        native = target
        if native.__code__.co_flags & (
            inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
        ):
            raise _definition_error("call_function", "generator targets are unsupported", ())
        annotations = _declared_annotations(native)
        adapter = CallAdapter(
            native,
            annotations=annotations,
            validate_return=validate_return,
            limits=resolved_limits,
            definition_limits=resolved_definition_limits,
            call_limits=resolved_call_limits,
        )
        wrapper: Callable[..., object]
        if native.__code__.co_flags & inspect.CO_COROUTINE:

            async def wrapped_async(*args: object, **kwargs: object) -> object:
                return await adapter.call_async(*args, **kwargs)

            wrapper = wrapped_async
        else:

            def wrapped_sync(*args: object, **kwargs: object) -> object:
                return adapter.call(*args, **kwargs)

            wrapper = wrapped_sync
        # functools.wraps copies __annotate__ on Python 3.14. Do not copy the
        # source function's dictionary, annotations or lazy evaluation hook.
        wrapped = cast(FunctionType, wrapper)
        wrapped.__module__ = native.__module__
        wrapped.__name__ = native.__name__
        wrapped.__qualname__ = native.__qualname__
        wrapped.__doc__ = native.__doc__
        wrapped.__annotations__ = {}
        wrapped.__dict__["__signature__"] = adapter.signature
        return wrapped

    if function is None:
        return decorate
    return decorate(function)


__all__ = ["native_validated_call"]
