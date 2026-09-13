"""Explicit-map, identity-preserving native calls with bounded pure validation."""

from __future__ import annotations

import asyncio
import inspect
import keyword
import types
from collections.abc import Callable, Coroutine
from dataclasses import InitVar, dataclass, field
from typing import TypedDict, cast

from .dataclass_adapter import (
    DataclassLimits,
    _check,
    _compile,
    _dispose_fresh_coroutine,
    _Plan,
    _preflight,
    _project,
    _Tree,
    _Work,
)
from .errors import PayloadValidationError, ValidationIssue
from .output_schema import OutputContractError, OutputLimits, _text
from .schema_io import SchemaDefinitionLimits, _definition_error

_P = inspect.Parameter
_FIXED_POSITIONAL = (_P.POSITIONAL_ONLY, _P.POSITIONAL_OR_KEYWORD)


@dataclass(frozen=True, slots=True)
class CallLimits:
    """Per-call admission/work limits, not Python callback or process RSS bounds."""

    max_parameters: int = 64
    max_arguments: int = 1_024
    max_keyword_characters: int = 16_384
    max_steps: int = 100_000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_parameters", 256),
            ("max_arguments", 100_000),
            ("max_keyword_characters", 1_000_000),
            ("max_steps", 1_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def _work(limits: OutputLimits, calls: CallLimits) -> _Work:
    return _Work(limits, DataclassLimits(max_steps=calls.max_steps, max_callbacks=1))


def _record_annotation(annotations: dict[str, object]) -> object:
    factory = cast(Callable[[str, dict[str, object]], type[object]], TypedDict)
    record = factory("_NativeCallRecord", annotations)
    # Owned generated metadata, not a read/evaluation/mutation of a caller's type.
    record.__annotations__ = annotations
    return record


def _shape(
    target: object, annotations: object, validate_return: bool, calls: CallLimits, work: _Work
) -> tuple[types.FunctionType, types.CodeType, tuple[_P, ...], inspect.Signature]:
    receiver = type(target) is types.MethodType
    function = cast(types.MethodType, target).__func__ if receiver else target
    if type(function) is not types.FunctionType:
        raise _definition_error("call_function", "requires a native Python function or method", ())
    code = function.__code__
    if code.co_flags & (
        inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR | inspect.CO_ITERABLE_COROUTINE
    ):
        raise _definition_error("call_function", "generator targets are unsupported", ())
    if receiver and code.co_argcount == 0:
        raise _definition_error("call_signature", "bound method needs a positional receiver", ())
    fixed = code.co_argcount + code.co_kwonlyargcount
    count = (
        fixed
        + bool(code.co_flags & inspect.CO_VARARGS)
        + bool(code.co_flags & inspect.CO_VARKEYWORDS)
    )
    if count - receiver > calls.max_parameters:
        raise _definition_error("call_signature", "parameter count exceeds limits", ())
    defaults = function.__defaults__
    keywords = function.__kwdefaults__
    if (
        defaults is not None and (type(defaults) is not tuple or len(defaults) > code.co_argcount)
    ) or (
        keywords is not None
        and (type(keywords) is not dict or len(keywords) > code.co_kwonlyargcount)
    ):
        raise _definition_error("call_signature", "malformed default metadata", ())
    defaults = () if defaults is None else defaults
    keywords = {} if keywords is None else keywords
    names = code.co_varnames[:count]
    for name in names:
        work.visit()
        if (
            type(name) is not str
            or len(name) > 256
            or not name.isidentifier()
            or keyword.iskeyword(name)
            or not _text(name)
        ):
            raise _definition_error("call_signature", "invalid parameter name", ())
    keyword_names = names[code.co_argcount : fixed]
    for name in keywords:
        work.visit()
        if type(name) is not str or name not in keyword_names:
            raise _definition_error("call_signature", "malformed keyword defaults", ())
    if type(annotations) is not dict or len(annotations) > count - receiver + 1:
        raise _definition_error("call_annotations", "requires a complete exact annotation map", ())
    mapping = cast(dict[object, object], annotations)
    exposed = names[1:] if receiver else names
    for annotation_name in mapping:
        work.visit()
        if type(annotation_name) is not str or (
            annotation_name != "return" and annotation_name not in exposed
        ):
            raise _definition_error(
                "call_annotations", "annotation keys must name exposed parameters", ()
            )
    if any(name not in mapping for name in exposed) or (
        validate_return and "return" not in mapping
    ):
        raise _definition_error("call_annotations", "required annotation is missing", ())
    parameters = []
    for index in range(code.co_argcount):
        if receiver and index == 0:
            continue
        name = names[index]
        default_index = index - (code.co_argcount - len(defaults))
        parameters.append(
            _P(
                name,
                _P.POSITIONAL_ONLY if index < code.co_posonlyargcount else _P.POSITIONAL_OR_KEYWORD,
                default=defaults[default_index] if default_index >= 0 else _P.empty,
                annotation=mapping[name],
            )
        )
    surplus_index = fixed
    if code.co_flags & inspect.CO_VARARGS:
        name = names[surplus_index]
        parameters.append(_P(name, _P.VAR_POSITIONAL, annotation=mapping[name]))
        surplus_index += 1
    parameters.extend(
        _P(name, _P.KEYWORD_ONLY, default=keywords.get(name, _P.empty), annotation=mapping[name])
        for name in keyword_names
    )
    if code.co_flags & inspect.CO_VARKEYWORDS:
        name = names[surplus_index]
        parameters.append(_P(name, _P.VAR_KEYWORD, annotation=mapping[name]))
    try:
        signature = inspect.Signature(parameters, return_annotation=mapping.get("return", _P.empty))
    except (ValueError, TypeError):
        raise _definition_error("call_signature", "malformed native signature", ()) from None
    return function, code, tuple(parameters), signature


def _binding_error() -> PayloadValidationError:
    return PayloadValidationError(
        (
            ValidationIssue(
                "call_bind", "arguments do not match the captured signature", '$["arguments"]'
            ),
        )
    )


@dataclass(frozen=True, slots=True, eq=False)
class CallAdapter:
    """Check borrowed native values, then call once; never evaluate target annotations.

    Explicit annotations replace inference. Defaults and values retain identity;
    input isolation, rollback, arbitrary callables and a decorator are not provided.
    """

    function: object = field(repr=False)
    annotations: InitVar[dict[str, object]] = field(kw_only=True)
    validate_return: bool = field(default=False, kw_only=True)
    limits: OutputLimits = field(default_factory=OutputLimits, kw_only=True)
    definition_limits: SchemaDefinitionLimits = field(
        default_factory=SchemaDefinitionLimits, kw_only=True
    )
    call_limits: CallLimits = field(default_factory=CallLimits, kw_only=True)
    signature: inspect.Signature = field(init=False, repr=False)
    _function: types.FunctionType = field(init=False, repr=False)
    _code: types.CodeType = field(init=False, repr=False)
    _parameters: tuple[_P, ...] = field(init=False, repr=False)
    _arguments_plan: _Plan = field(init=False, repr=False)
    _return_plan: _Plan | None = field(init=False, repr=False)

    def __post_init__(self, annotations: dict[str, object]) -> None:
        if (
            type(self.validate_return) is not bool
            or type(self.limits) is not OutputLimits
            or type(self.definition_limits) is not SchemaDefinitionLimits
            or type(self.call_limits) is not CallLimits
        ):
            raise ValueError("call options/limits must have their declared types")
        work = _work(self.limits, self.call_limits)
        function, code, parameters, signature = _shape(
            self.function, annotations, self.validate_return, self.call_limits, work
        )
        parameter_annotations = {}
        for item in parameters:
            work.visit()
            annotation = item.annotation
            if item.kind is _P.VAR_POSITIONAL:
                annotation = types.GenericAlias(tuple, (annotation, Ellipsis))
            elif item.kind is _P.VAR_KEYWORD:
                annotation = types.GenericAlias(dict, (str, annotation))
            parameter_annotations[item.name] = annotation
        forest_annotations = {"arguments": _record_annotation(parameter_annotations)}
        if "return" in annotations:
            forest_annotations["return"] = annotations["return"]
        forest = _compile(
            _record_annotation(forest_annotations), self.definition_limits, native_only=True
        )
        arguments_plan = forest.fields[0].plan
        return_plan = forest.fields[1].plan if len(forest.fields) == 2 else None
        tree = _Tree(self.limits)
        tree.visit(0, {})
        for item, member in zip(parameters, arguments_plan.fields, strict=True):
            work.visit()
            if item.default is not _P.empty:
                tree.text(item.name)
                path = ("arguments", item.name)
                projected = _project(member.plan, item.default, work, path, tree, depth=1)
                _check(member.plan.output_schema, projected, work, path)
                _preflight(member.plan, projected, work, path)
        object.__setattr__(self, "_function", function)
        object.__setattr__(self, "_code", code)
        object.__setattr__(self, "_parameters", parameters)
        object.__setattr__(self, "signature", signature)
        object.__setattr__(self, "_arguments_plan", arguments_plan)
        object.__setattr__(self, "_return_plan", return_plan)

    def _bind(
        self, args: tuple[object, ...], kwargs: dict[str, object], work: _Work
    ) -> dict[str, object]:
        if len(args) + len(kwargs) > self.call_limits.max_arguments:
            raise OutputContractError("raw call argument count exceeds limits")
        characters = 0
        for _ in args:
            work.visit()
        for name in kwargs:
            work.visit()
            if type(name) is not str:
                raise OutputContractError("call keywords require exact built-in strings")
            characters += len(name)
            if (
                len(name) > 256
                or characters > self.call_limits.max_keyword_characters
                or not _text(name)
            ):
                raise OutputContractError("call keyword character or Unicode limit exceeded")
        bound: dict[str, object] = {}
        named = {}
        varkw: str | None = None
        position = 0
        for item in self._parameters:
            work.visit()
            if item.kind in _FIXED_POSITIONAL and position < len(args):
                bound[item.name] = args[position]
                position += 1
            elif item.kind is _P.VAR_POSITIONAL:
                bound[item.name] = args[position:]
                position = len(args)
            elif item.kind is _P.VAR_KEYWORD:
                varkw = item.name
            if item.kind in (_P.POSITIONAL_OR_KEYWORD, _P.KEYWORD_ONLY):
                named[item.name] = item
        if position != len(args):
            raise _binding_error()
        extras = {}
        for name, value in kwargs.items():
            work.visit()
            if name in named:
                if name in bound:
                    raise _binding_error()
                bound[name] = value
            elif varkw is not None:
                extras[name] = value
            else:
                raise _binding_error()
        if varkw is not None:
            bound[varkw] = extras
        completed = 0
        for item in self._parameters:
            work.visit()
            if item.name not in bound:
                if item.default is _P.empty:
                    raise _binding_error()
                work.visit()
                bound[item.name] = item.default
            value = bound[item.name]
            completed += (
                len(cast(tuple[object, ...] | dict[str, object], value))
                if item.kind in (_P.VAR_POSITIONAL, _P.VAR_KEYWORD)
                else 1
            )
        if completed > self.call_limits.max_arguments:
            raise OutputContractError("completed call argument count exceeds limits")
        return bound

    def _prepare_call(
        self, args: tuple[object, ...], kwargs: dict[str, object], *, asynchronous: bool
    ) -> tuple[tuple[object, ...], dict[str, object], _Work, _Tree]:
        if self._function.__code__ is not self._code:
            raise OutputContractError("target code changed after compilation")
        if bool(self._code.co_flags & inspect.CO_COROUTINE) != asynchronous:
            raise OutputContractError("call method does not match the native target execution mode")
        work = _work(self.limits, self.call_limits)
        bound = self._bind(args, kwargs, work)
        tree = _Tree(self.limits)
        path = ("arguments",)
        projection = _project(self._arguments_plan, bound, work, path, tree)
        _check(self._arguments_plan.output_schema, projection, work, path)
        _preflight(self._arguments_plan, projection, work, path)
        positional = []
        keywords = {}
        for item in self._parameters:
            work.visit()
            value = bound[item.name]
            if item.kind in _FIXED_POSITIONAL:
                positional.append(value)
            elif item.kind is _P.KEYWORD_ONLY:
                keywords[item.name] = value
            elif item.kind is _P.VAR_POSITIONAL:
                for child in cast(tuple[object, ...], value):
                    work.visit()
                    positional.append(child)
            else:
                for name, child in cast(dict[str, object], value).items():
                    work.visit()
                    keywords[name] = child
        prepared = tuple(positional), keywords, work, tree
        if self._function.__code__ is not self._code:
            raise OutputContractError("target code changed during argument validation")
        work.callback()
        return prepared

    def _result(self, value: object, work: _Work, tree: _Tree) -> object:
        if self.validate_return:
            plan = cast(_Plan, self._return_plan)
            try:
                projected = _project(plan, value, work, ("return",), tree)
                _check(plan.output_schema, projected, work, ("return",))
                _preflight(plan, projected, work, ("return",))
            except (PayloadValidationError, OutputContractError):
                _dispose_fresh_coroutine(value)
                raise
        return value

    def call(self, /, *args: object, **kwargs: object) -> object:
        """Validate native arguments, call a sync target once, optionally check its result."""
        positional, keywords, work, tree = self._prepare_call(args, kwargs, asynchronous=False)
        result = cast(Callable[..., object], self.function)(*positional, **keywords)
        return self._result(result, work, tree)

    async def call_async(self, /, *args: object, **kwargs: object) -> object:
        """Lazily check then directly await a native coroutine target in the caller task."""
        await asyncio.sleep(0)
        positional, keywords, work, tree = self._prepare_call(args, kwargs, asynchronous=True)
        target = cast(Callable[..., Coroutine[object, object, object]], self.function)
        result = await target(*positional, **keywords)
        return self._result(result, work, tree)
