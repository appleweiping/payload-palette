"""Explicit trusted dataclass construction over the existing bounded JSON engine."""

from __future__ import annotations

import dataclasses
import inspect
import types
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import (
    Annotated,
    ClassVar,
    Generic,
    TypeVar,
    Union,
    cast,
    get_args,
    get_origin,
    is_typeddict,
)

from .annotation_adapter import (
    JSONSerializationOptions,
    _BuildAnnotation,
    _compile_schema,
    _encode_json,
)
from .errors import PayloadValidationError, ValidationIssue
from .ingress import decode_json_bytes
from .output_schema import (
    JSONScalar,
    JSONValue,
    OutputContractError,
    OutputLimits,
    OutputPath,
    OutputSchema,
    _number,
    _SchemaWork,
    _text,
    output_path,
    snapshot_json,
)
from .scalar_fields import _codec_for, _ScalarCodec
from .schema_io import SchemaDefinitionLimits, _definition_error

_T = TypeVar("_T")
_ABSENT = object()


@dataclass(frozen=True, slots=True)
class DataclassLimits:
    """Aggregate adapter work, direct callback count and compiled literal storage.

    These bounds cannot preempt trusted Python callbacks or limit allocations
    and side effects performed inside them.
    """

    max_steps: int = 100_000
    max_callbacks: int = 1_000
    max_default_bytes: int = 4_000_000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_steps", 1_000_000),
            ("max_callbacks", 10_000),
            ("max_default_bytes", 32_000_000),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")


@dataclass(frozen=True, slots=True)
class _LiteralDefault:
    raw: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class _Field:
    name: str
    plan: _Plan
    default: object = field(default=_ABSENT, repr=False)
    factory: Callable[[], object] | None = field(default=None, repr=False)


@dataclass(frozen=True, slots=True)
class _Plan:
    schema: OutputSchema
    output_schema: OutputSchema
    kind: str = "value"
    children: tuple[_Plan, ...] = ()
    fields: tuple[_Field, ...] = ()
    model: type[object] | None = field(default=None, repr=False)
    codec: _ScalarCodec | None = field(default=None, repr=False)
    constructs: bool = field(init=False)
    converts: bool = field(init=False)

    def __post_init__(self) -> None:
        # Cache this definition property once; repeated runtime union selection
        # must not traverse the entire annotation tree outside its work counter.
        object.__setattr__(
            self,
            "constructs",
            self.model is not None
            or self.codec is not None
            or any(child.constructs for child in self.children)
            or any(item.plan.constructs for item in self.fields),
        )
        object.__setattr__(
            self,
            "converts",
            self.codec is not None
            or any(child.converts for child in self.children)
            or any(item.plan.converts for item in self.fields),
        )


@dataclass(slots=True)
class _Work:
    limits: OutputLimits
    construction: DataclassLimits
    schema: _SchemaWork = field(default_factory=_SchemaWork)
    steps: int = 0
    callbacks: int = 0

    def visit(self) -> None:
        self.steps += 1
        if self.steps > self.construction.max_steps:
            raise OutputContractError("dataclass materialization work limit exceeded")

    def callback(self) -> None:
        self.callbacks += 1
        if self.callbacks > min(self.construction.max_callbacks, self.limits.max_invocations):
            raise OutputContractError("dataclass callback limit exceeded")


@dataclass(slots=True)
class _Tree:
    limits: OutputLimits
    nodes: int = 0
    characters: int = 0

    def visit(self, depth: int, value: object) -> None:
        self.nodes += 1
        if self.nodes > self.limits.max_nodes or depth > self.limits.max_depth:
            raise OutputContractError("dataclass snapshot node or depth limit exceeded")
        if type(value) is str:
            self.text(value)
        if (
            type(value) in (list, dict)
            and len(cast(list[object], value)) > self.limits.max_nodes - self.nodes
        ):
            raise OutputContractError("dataclass snapshot node limit exceeded")

    def text(self, value: str) -> None:
        self.characters += len(value)
        if self.characters > self.limits.max_characters or not _text(value):
            raise OutputContractError("dataclass snapshot character or Unicode limit exceeded")


def _problem(code: str, message: str, path: OutputPath) -> PayloadValidationError:
    return PayloadValidationError((ValidationIssue(code, message, output_path(path)),))


def _check(schema: OutputSchema, value: JSONValue, work: _Work, path: OutputPath = ()) -> None:
    issues = schema._validate_snapshot(value, work.limits, work.schema, path)
    if issues:
        raise PayloadValidationError(issues)


def _known_async(callback: object) -> bool:
    if isinstance(callback, (classmethod, staticmethod)):
        callback = callback.__func__
    return inspect.iscoroutinefunction(callback) or inspect.isasyncgenfunction(callback)


def _constructor_async(model: type[object]) -> bool:
    return any(
        _known_async(getattr(model, name, None))
        for name in ("__new__", "__init__", "__post_init__")
    ) or _known_async(inspect.getattr_static(type(model), "__call__", None))


def _dispose_fresh_coroutine(value: object) -> None:
    # Only an unstarted native coroutine returned at our callback boundary is
    # owned here. Tasks, Futures, started coroutines and custom awaitables remain
    # caller-owned; never cancel/advance/close them to make them fit a type.
    if inspect.iscoroutine(value) and inspect.getcoroutinestate(value) == inspect.CORO_CREATED:
        value.close()


def _compile(model: type[object], limits: SchemaDefinitionLimits) -> _Plan:
    by_schema: dict[int, _Plan] = {}
    by_annotation: dict[int, _Plan] = {}

    def compiled(annotation: object, schema: OutputSchema) -> None:
        if id(schema) in by_schema:
            by_annotation[id(annotation)] = by_schema[id(schema)]
            return
        origin = get_origin(annotation)
        output = schema
        children: tuple[_Plan, ...] = ()
        fields: tuple[_Field, ...] = ()
        kind = "value"
        if origin is Annotated:
            base = by_annotation[id(get_args(annotation)[0])]
            output = replace(
                base.output_schema,
                minimum=schema.minimum,
                maximum=schema.maximum,
                min_length=schema.min_length,
                max_length=schema.max_length,
            )
            plan = replace(base, schema=schema, output_schema=output)
        else:
            if origin is list:
                kind, children = "array", (by_schema[id(schema.items)],)
                output = replace(schema, items=children[0].output_schema)
            elif origin is dict:
                kind, children = "map", (by_schema[id(schema.additional_properties)],)
                output = replace(schema, additional_properties=children[0].output_schema)
            elif origin in (Union, types.UnionType):
                kind, children = "union", tuple(by_schema[id(child)] for child in schema.any_of)
                output = replace(schema, any_of=tuple(child.output_schema for child in children))
            elif is_typeddict(annotation):
                kind = "record"
                fields = tuple(
                    _Field(name, by_schema[id(child)]) for name, child in schema.properties.items()
                )
                output = replace(
                    schema, properties={item.name: item.plan.output_schema for item in fields}
                )
            plan = _Plan(schema, output, kind, children, fields)
        by_schema[id(schema)] = plan
        by_annotation[id(annotation)] = plan

    def custom(
        current: object, depth: int, path: OutputPath, build: _BuildAnnotation
    ) -> OutputSchema | None:
        codec = _codec_for(current)
        if codec is not None:
            schema = OutputSchema("string", min_length=1, max_length=codec.maximum)
            by_schema[id(schema)] = _Plan(schema, schema, "scalar", codec=codec)
            return schema
        if not isinstance(current, type) or not dataclasses.is_dataclass(current):
            return None
        metadata = inspect.getattr_static(current, "__dataclass_fields__", None)
        if (
            type(metadata) is not dict
            or len(metadata) > 256
            or inspect.getattr_static(current, "__parameters__", ())
        ):
            raise _definition_error(
                "dataclass_definition", "malformed, generic or oversized dataclass", path
            )
        params = inspect.getattr_static(current, "__dataclass_params__", None)
        if getattr(params, "init", None) is not True or _constructor_async(current):
            raise _definition_error(
                "dataclass_constructor", "dataclass needs a synchronous init constructor", path
            )
        members = []
        required = []
        for name, item in metadata.items():
            if type(item) is not dataclasses.Field or type(name) is not str or item.name != name:
                raise _definition_error(
                    "dataclass_field", "malformed dataclass field metadata", path
                )
            annotation = item.type
            if annotation is ClassVar or get_origin(annotation) is ClassVar:
                continue
            if (
                annotation is dataclasses.InitVar
                or isinstance(annotation, dataclasses.InitVar)
                or item.init is not True
                or item.metadata
            ):
                raise _definition_error(
                    "dataclass_field",
                    "InitVar, init=False and field metadata are unsupported",
                    (*path, name),
                )
            if not _text(name) or not name.isidentifier() or len(name) > 256:
                raise _definition_error("dataclass_field", "invalid or oversized field name", path)
            descriptor = inspect.getattr_static(current, name, _ABSENT)
            if (
                inspect.getattr_static(type(descriptor), "__get__", None) is not None
                and type(descriptor) is not types.MemberDescriptorType
            ):
                raise _definition_error(
                    "dataclass_descriptor",
                    "custom field descriptors are unsupported",
                    (*path, name),
                )
            child = build(annotation, depth + 1, (*path, name))
            default = _ABSENT if item.default is dataclasses.MISSING else item.default
            has_factory = item.default_factory is not dataclasses.MISSING
            factory = item.default_factory if has_factory else None
            if has_factory and (
                not callable(factory)
                or _known_async(factory)
                or _known_async(inspect.getattr_static(type(factory), "__call__", None))
            ):
                raise _definition_error(
                    "dataclass_factory",
                    "default factory must be synchronous and callable",
                    (*path, name),
                )
            if default is not _ABSENT and factory is not None:
                raise _definition_error(
                    "dataclass_default",
                    "default and factory cannot both be specified",
                    (*path, name),
                )
            if default is _ABSENT and factory is None:
                required.append(name)
            members.append(
                _Field(
                    name, by_schema[id(child)], default, cast(Callable[[], object] | None, factory)
                )
            )
        try:
            inspect.signature(current).bind(**{item.name: None for item in members})
        except (TypeError, ValueError) as exc:
            raise _definition_error(
                "dataclass_constructor",
                "constructor must accept all declared fields as keywords",
                path,
            ) from exc
        schema = OutputSchema(
            "object",
            properties={item.name: item.plan.schema for item in members},
            required=tuple(required),
        )
        output = OutputSchema(
            "object",
            properties={item.name: item.plan.output_schema for item in members},
            required=tuple(item.name for item in members),
        )
        by_schema[id(schema)] = _Plan(
            schema, output, "dataclass", fields=tuple(members), model=current
        )
        return schema

    schema = _compile_schema(model, limits=limits, custom=custom, compiled=compiled)
    return by_schema[id(schema)]


def _select(plan: _Plan, value: JSONValue, work: _Work, path: OutputPath) -> _Plan:
    matches = []
    for child in plan.children:
        work.visit()
        if not child.schema._validate_snapshot(value, work.limits, work.schema, path):
            if child.converts:
                try:
                    _preflight(child, value, work, path)
                except PayloadValidationError:
                    continue
            matches.append(child)
    if not matches:
        raise _problem("dataclass_union", "value does not match a union branch", path)
    if len(matches) > 1 and plan.constructs:
        raise _problem(
            "dataclass_union_ambiguous", "constructed union needs one structural match", path
        )
    return matches[0]


def _preflight(plan: _Plan, value: JSONValue, work: _Work, path: OutputPath = ()) -> None:
    work.visit()
    if plan.codec is not None:
        _scalar_value(plan.codec, value, path)
    elif plan.kind == "union":
        _preflight(_select(plan, value, work, path), value, work, path)
    elif plan.kind == "array":
        for position, child in enumerate(cast(list[JSONValue], value)):
            _preflight(plan.children[0], child, work, (*path, position))
    elif plan.kind == "map":
        for name, child in cast(dict[str, JSONValue], value).items():
            _preflight(plan.children[0], child, work, (*path, name))
    elif plan.kind in ("record", "dataclass"):
        document = cast(dict[str, JSONValue], value)
        for item in plan.fields:
            if item.name in document:
                _preflight(item.plan, document[item.name], work, (*path, item.name))


def _scalar_value(
    codec: _ScalarCodec, value: object, path: OutputPath, *, dump: bool = False
) -> object:
    try:
        return codec.encode(value) if dump else codec.decode(value)
    except (ValueError, TypeError, OverflowError):
        raise _problem("dataclass_scalar", f"invalid {codec.name} field", path) from None


def _project(
    plan: _Plan,
    value: object,
    work: _Work,
    path: OutputPath = (),
    tree: _Tree | None = None,
    active: set[int] | None = None,
    depth: int = 0,
) -> JSONValue:
    work.visit()
    tree = _Tree(work.limits) if tree is None else tree
    active = set() if active is None else active
    if plan.kind == "union":
        # Probe type shape without executing constructors/factories. Every
        # attempt shares work; resource exhaustion is not a branch mismatch.
        for child in plan.children:
            try:
                projected = _project(child, value, work, path, _Tree(work.limits), active, depth)
                _check(child.output_schema, projected, work, path)
            except PayloadValidationError:
                continue
            selected = _select(plan, projected, work, path)
            return _project(selected, value, work, path, tree, active, depth)
        raise _problem("dataclass_type", "object does not match a union's declared types", path)
    tree.visit(depth, value)
    if plan.codec is not None:
        encoded = cast(str, _scalar_value(plan.codec, value, path, dump=True))
        tree.text(encoded)
        return encoded
    if plan.kind == "value":
        if value is None or type(value) in (str, bool) or _number(value):
            return cast(JSONScalar, value)
        raise _problem("dataclass_type", "expected a finite JSON scalar", path)
    if id(value) in active:
        raise OutputContractError("cyclic dataclass object graph")
    active.add(id(value))
    try:
        if plan.kind == "array":
            if type(value) is not list:
                raise _problem("dataclass_type", "expected a built-in list", path)
            return [
                _project(plan.children[0], child, work, (*path, index), tree, active, depth + 1)
                for index, child in enumerate(value)
            ]
        if plan.kind == "dataclass":
            if type(value) is not plan.model:
                raise _problem("dataclass_type", "expected the exact declared dataclass", path)
            source = {}
            for item in plan.fields:
                try:
                    source[item.name] = object.__getattribute__(value, item.name)
                except Exception:
                    raise _problem(
                        "dataclass_attribute",
                        "declared field could not be read",
                        (*path, item.name),
                    ) from None
        else:
            if type(value) is not dict:
                raise _problem("dataclass_type", "expected a built-in dictionary", path)
            source = value
        result = {}
        declared = {item.name: item for item in plan.fields}
        for name, child in source.items():
            if type(name) is not str:
                raise _problem("dataclass_key", "field keys must be strings", path)
            tree.text(name)
            if plan.kind == "map":
                child_plan = plan.children[0]
            elif name in declared:
                child_plan = declared[name].plan
            else:
                raise _problem("dataclass_extra", "undeclared field", path)
            result[name] = _project(child_plan, child, work, (*path, name), tree, active, depth + 1)
        return result
    finally:
        active.remove(id(value))


def _freeze_defaults(plan: _Plan, work: _Work) -> _Plan:
    remaining = work.construction.max_default_bytes

    def freeze(node: _Plan, path: OutputPath) -> _Plan:
        nonlocal remaining
        work.visit()
        children = tuple(freeze(child, path) for child in node.children)
        fields = []
        for item in node.fields:
            item_path = (*path, item.name)
            child = freeze(item.plan, item_path)
            default = item.default
            if default is not _ABSENT:
                document = _project(child, default, work, item_path)
                _check(child.output_schema, document, work, item_path)
                _preflight(child, document, work, item_path)
                if remaining < 1:
                    raise OutputContractError("compiled default byte limit exceeded")
                raw = _encode_json(document, JSONSerializationOptions(max_output_bytes=remaining))
                remaining -= len(raw)
                default = _LiteralDefault(raw)
            fields.append(replace(item, plan=child, default=default))
        return replace(node, children=children, fields=tuple(fields))

    return freeze(plan, ())


def _factory(item: _Field, work: _Work, path: OutputPath) -> JSONValue:
    work.callback()
    try:
        # _prepare admits this branch only when a compiled factory is present.
        candidate = cast(Callable[[], object], item.factory)()
        _dispose_fresh_coroutine(candidate)
    except Exception:
        raise _problem(
            "dataclass_factory", "default factory raised or returned an invalid result", path
        ) from None
    value = _project(item.plan, candidate, work, path)
    _check(item.plan.output_schema, value, work, path)
    _preflight(item.plan, value, work, path)
    return value


@dataclass(frozen=True, slots=True)
class _Ready:
    plan: _Plan
    value: object = None
    items: tuple[_Ready, ...] = ()
    fields: tuple[tuple[str, _Ready], ...] = ()


def _prepare(
    plan: _Plan, value: JSONValue, work: _Work, tree: _Tree, path: OutputPath = (), depth: int = 0
) -> _Ready:
    work.visit()
    if plan.kind == "union":
        return _prepare(_select(plan, value, work, path), value, work, tree, path, depth)
    if plan.codec is not None:
        converted = _scalar_value(plan.codec, value, path)
        # Budget the actual output representation before adapter constructors.
        canonical = _scalar_value(plan.codec, converted, path, dump=True)
        tree.visit(depth, canonical)
        return _Ready(plan, converted)
    tree.visit(depth, value)
    if plan.kind == "value":
        return _Ready(plan, cast(JSONScalar, value))
    if plan.kind == "array":
        return _Ready(
            plan,
            items=tuple(
                _prepare(plan.children[0], child, work, tree, (*path, index), depth + 1)
                for index, child in enumerate(cast(list[JSONValue], value))
            ),
        )
    document = cast(dict[str, JSONValue], value)
    result = []
    if plan.kind == "map":
        members = tuple(_Field(name, plan.children[0]) for name in document)
    else:
        members = plan.fields
    for item in members:
        item_path = (*path, item.name)
        if item.name in document:
            child_value = document[item.name]
        elif type(item.default) is _LiteralDefault:
            child_value = decode_json_bytes(
                item.default.raw, max_input_bytes=work.construction.max_default_bytes
            )
        elif item.factory is not None:
            child_value = _factory(item, work, item_path)
        else:
            continue  # Only a genuinely optional TypedDict key can be absent here.
        tree.text(item.name)
        result.append(
            (item.name, _prepare(item.plan, child_value, work, tree, item_path, depth + 1))
        )
    return _Ready(plan, fields=tuple(result))


def _construct(ready: _Ready, work: _Work, path: OutputPath = ()) -> object:
    work.visit()
    if ready.plan.kind in ("value", "scalar"):
        return ready.value
    if ready.plan.kind == "array":
        return [_construct(item, work, (*path, index)) for index, item in enumerate(ready.items)]
    fields = {name: _construct(item, work, (*path, name)) for name, item in ready.fields}
    model = ready.plan.model
    if model is None:
        return fields
    work.callback()
    try:
        if _constructor_async(model):
            raise ValueError("constructor became asynchronous")
        result = model(**fields)
        _dispose_fresh_coroutine(result)
        if type(result) is not model:
            raise ValueError("constructor returned another type")
    except Exception:
        raise _problem(
            "dataclass_constructor", "constructor raised or returned an invalid result", path
        ) from None
    return result


@dataclass(frozen=True, slots=True)
class DataclassAdapter(Generic[_T]):
    """Construct real trusted dataclasses; input JSON and returned objects are distinct.

    Default factories and constructors are explicit trusted synchronous callbacks.
    Dumping reads declared fields and never invokes those callbacks. Returned
    models retain their class's mutability; no assignment-validation is installed.
    """

    model: type[_T] = field(repr=False)
    limits: OutputLimits = field(default_factory=OutputLimits)
    definition_limits: SchemaDefinitionLimits = field(default_factory=SchemaDefinitionLimits)
    construction: DataclassLimits = field(default_factory=DataclassLimits)
    serialization: JSONSerializationOptions = field(default_factory=JSONSerializationOptions)
    input_schema: OutputSchema = field(init=False)
    output_schema: OutputSchema = field(init=False)
    _plan: _Plan = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.limits) is not OutputLimits
            or type(self.definition_limits) is not SchemaDefinitionLimits
            or type(self.construction) is not DataclassLimits
            or type(self.serialization) is not JSONSerializationOptions
        ):
            raise ValueError("adapter limits/options must have their declared types")
        if not isinstance(self.model, type) or not dataclasses.is_dataclass(self.model):
            raise _definition_error(
                "dataclass_root", "adapter requires a concrete dataclass type", ()
            )
        plan = _compile(self.model, self.definition_limits)
        plan = _freeze_defaults(plan, _Work(self.limits, self.construction))
        object.__setattr__(self, "_plan", plan)
        object.__setattr__(self, "input_schema", plan.schema)
        object.__setattr__(self, "output_schema", plan.output_schema)

    def validate_python(self, value: object) -> _T:
        work = _Work(self.limits, self.construction)
        document = snapshot_json(value, self.limits)
        _check(self.input_schema, document, work)
        _preflight(self._plan, document, work)
        ready = _prepare(self._plan, document, work, _Tree(self.limits))
        model = _construct(ready, work)
        output = _project(self._plan, model, work)
        _check(self.output_schema, output, work)
        _preflight(self._plan, output, work)
        return cast(_T, model)

    def validate_json_bytes(
        self, payload: bytes | bytearray | memoryview, *, max_input_bytes: int = 4_000_000
    ) -> _T:
        return self.validate_python(decode_json_bytes(payload, max_input_bytes=max_input_bytes))

    def dump_python(self, value: object) -> JSONValue:
        work = _Work(self.limits, self.construction)
        document = _project(self._plan, value, work)
        _check(self.output_schema, document, work)
        _preflight(self._plan, document, work)
        return document

    def dump_json(self, value: object) -> bytes:
        return _encode_json(self.dump_python(value), self.serialization)
