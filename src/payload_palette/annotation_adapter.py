"""Compile explicit trusted annotations into strict JSON contracts and serializers."""

from __future__ import annotations

import json
import types
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import (
    Annotated,
    ForwardRef,
    Literal,
    NotRequired,
    Required,
    Union,
    cast,
    get_args,
    get_origin,
    is_typeddict,
)

from payload_palette.errors import PayloadValidationError
from payload_palette.ingress import decode_json_bytes
from payload_palette.output_schema import (
    JSONScalar,
    JSONValue,
    OutputContractError,
    OutputLimits,
    OutputPath,
    OutputSchema,
    SchemaKind,
    _number,
    _text,
    snapshot_json,
)
from payload_palette.schema_io import (
    SchemaDefinitionError,
    SchemaDefinitionLimits,
    _definition_error,
    _definition_limits,
)


@dataclass(frozen=True, slots=True)
class FieldConstraints:
    """Explicit Annotated metadata; constraints are never inferred from arbitrary objects."""

    minimum: int | float | None = None
    maximum: int | float | None = None
    min_length: int | None = None
    max_length: int | None = None

    def __post_init__(self) -> None:
        if any(value is not None and not _number(value) for value in (self.minimum, self.maximum)):
            raise ValueError("numeric constraints require bounded finite numbers")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("minimum exceeds maximum")
        if any(
            value is not None and (type(value) is not int or not 0 <= value <= 8_000_000)
            for value in (self.min_length, self.max_length)
        ):
            raise ValueError("length constraints require bounded nonnegative integers")
        if (
            self.min_length is not None
            and self.max_length is not None
            and self.min_length > self.max_length
        ):
            raise ValueError("min_length exceeds max_length")


def _expanded_budget(schema: OutputSchema, limits: SchemaDefinitionLimits) -> None:
    nodes = 0
    characters = 0
    stack = [(schema, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        characters += sum(len(key) for key in current.properties)
        characters += sum(len(value) for value in current.enum or () if type(value) is str)
        if (
            nodes > limits.max_nodes
            or depth > limits.max_depth
            or characters > limits.max_characters
            or len(current.any_of) > limits.max_branches
        ):
            raise _definition_error(
                "annotation_budget", "expanded annotation schema exceeds limits", ()
            )
        stack.extend((child, depth + 1) for child in current.properties.values())
        stack.extend((child, depth + 1) for child in current.any_of)
        if current.items is not None:
            stack.append((current.items, depth + 1))
        if type(current.additional_properties) is OutputSchema:
            stack.append((current.additional_properties, depth + 1))


def schema_for_annotation(
    annotation: object, *, limits: SchemaDefinitionLimits | None = None
) -> OutputSchema:
    """Compile JSON primitives, lists, string-key maps, unions, Literal and TypedDict.

    Only trusted Python annotation objects are accepted. Strings and ForwardRefs
    are rejected rather than evaluated; get_type_hints, eval and schema-directed
    imports are never used. TypedDict compilation snapshots its annotations.
    """

    return _compile_schema(annotation, limits=limits)


_BuildAnnotation = Callable[[object, int, OutputPath], OutputSchema]
_CustomAnnotation = Callable[[object, int, OutputPath, _BuildAnnotation], OutputSchema | None]
_CompiledAnnotation = Callable[[object, OutputSchema], None]


def _compile_schema(
    annotation: object,
    *,
    limits: SchemaDefinitionLimits | None = None,
    custom: _CustomAnnotation | None = None,
    compiled: _CompiledAnnotation | None = None,
) -> OutputSchema:
    """Shared private compiler; the public JSON adapter never enables custom records."""
    active_limits = _definition_limits(limits)
    active: set[int] = set()
    nodes = 0

    def build(current: object, depth: int, path: OutputPath) -> OutputSchema:
        schema = build_node(current, depth, path)
        if compiled is not None:
            compiled(current, schema)
        return schema

    def build_node(current: object, depth: int, path: OutputPath) -> OutputSchema:
        nonlocal nodes
        nodes += 1
        if nodes > active_limits.max_nodes or depth > active_limits.max_depth:
            raise _definition_error(
                "annotation_budget", "annotation compiler work budget exceeded", path
            )
        if type(current) is str or isinstance(current, ForwardRef):
            raise _definition_error(
                "annotation_unresolved",
                "annotation strings and ForwardRefs are not evaluated",
                path,
            )
        if id(current) in active:
            raise _definition_error(
                "annotation_cycle", "recursive annotation is not supported", path
            )
        active.add(id(current))
        try:
            primitives: tuple[tuple[object, SchemaKind], ...] = (
                (str, "string"),
                (int, "integer"),
                (float, "number"),
                (bool, "boolean"),
                (type(None), "null"),
                (None, "null"),
            )
            for primitive, kind in primitives:
                if current is primitive:
                    return OutputSchema(kind)
            origin = get_origin(current)
            args = get_args(current)
            if origin is list and len(args) == 1:
                return OutputSchema("array", items=build(args[0], depth + 1, path))
            if origin is dict and len(args) == 2 and args[0] is str:
                return OutputSchema("object", additional_properties=build(args[1], depth + 1, path))
            if origin in (Union, types.UnionType):
                if not 2 <= len(args) <= active_limits.max_branches:
                    raise _definition_error(
                        "annotation_branches", "union has too many alternatives", path
                    )
                return OutputSchema(
                    "union", any_of=tuple(build(item, depth + 1, path) for item in args)
                )
            if origin is Literal:
                if not 1 <= len(args) <= 256 or any(
                    type(item) not in (str, int, bool, type(None)) for item in args
                ):
                    raise _definition_error(
                        "annotation_literal",
                        "Literal requires bounded JSON string/int/bool/null choices",
                        path,
                    )
                grouped: dict[type[object], list[JSONScalar]] = {}
                for item in args:
                    grouped.setdefault(type(item), []).append(item)
                kinds: dict[type[object], SchemaKind] = {
                    str: "string",
                    int: "integer",
                    bool: "boolean",
                    type(None): "null",
                }
                branches = tuple(
                    OutputSchema(kinds[kind], enum=tuple(values))
                    for kind, values in grouped.items()
                )
                return branches[0] if len(branches) == 1 else OutputSchema("union", any_of=branches)
            if origin is Annotated:
                if len(args) != 2 or type(args[1]) is not FieldConstraints:
                    raise _definition_error(
                        "annotation_metadata",
                        "Annotated accepts exactly one FieldConstraints object",
                        path,
                    )
                base = build(args[0], depth + 1, path)
                constraint = args[1]
                return replace(
                    base,
                    minimum=constraint.minimum,
                    maximum=constraint.maximum,
                    min_length=constraint.min_length,
                    max_length=constraint.max_length,
                )
            if is_typeddict(current):
                annotations = getattr(current, "__annotations__", None)
                required_keys = getattr(current, "__required_keys__", None)
                if (
                    type(annotations) is not dict
                    or len(annotations) > 256
                    or type(required_keys) is not frozenset
                    or len(required_keys) > 256
                ):
                    raise _definition_error(
                        "annotation_typeddict", "TypedDict metadata is malformed or oversized", path
                    )
                if any(
                    type(key) is not str or len(key) > 256 or not _text(key) for key in annotations
                ):
                    raise _definition_error(
                        "annotation_typeddict",
                        "TypedDict field names must be bounded Unicode strings",
                        path,
                    )
                if any(type(key) is not str or key not in annotations for key in required_keys):
                    raise _definition_error(
                        "annotation_typeddict",
                        "TypedDict required keys must name declared fields",
                        path,
                    )
                properties: dict[str, OutputSchema] = {}
                required: list[str] = []
                for key, value in annotations.items():
                    wrapper: object = get_origin(cast(object, value))
                    mandatory = key in required_keys
                    if wrapper in (Required, NotRequired):
                        mandatory = wrapper is Required
                        value = get_args(value)[0]
                    properties[key] = build(value, depth + 1, (*path, key))
                    if mandatory:
                        required.append(key)
                return OutputSchema("object", properties=properties, required=tuple(required))
            if custom is not None:
                extension = custom(current, depth, path, build)
                if extension is not None:
                    return extension
            raise _definition_error("annotation_type", "unsupported Python annotation object", path)
        except SchemaDefinitionError:
            raise
        except ValueError as exc:
            raise _definition_error(
                "annotation_constraint", "annotation has incompatible or invalid constraints", path
            ) from exc
        finally:
            active.remove(id(current))

    schema = build(annotation, 0, ())
    _expanded_budget(schema, active_limits)
    return schema


@dataclass(frozen=True, slots=True)
class JSONSerializationOptions:
    """Presentation options only: serialization never coerces values or drops fields."""

    ensure_ascii: bool = False
    sort_keys: bool = True
    indent: int | None = None
    max_output_bytes: int = 4_000_000

    def __post_init__(self) -> None:
        if type(self.ensure_ascii) is not bool or type(self.sort_keys) is not bool:
            raise ValueError("ensure_ascii and sort_keys must be booleans")
        if self.indent is not None and (type(self.indent) is not int or not 0 <= self.indent <= 8):
            raise ValueError("indent must be None or an integer between 0 and 8")
        if type(self.max_output_bytes) is not int or not 1 <= self.max_output_bytes <= 32_000_000:
            raise ValueError("max_output_bytes must be an integer between 1 and 32000000")


@dataclass(frozen=True, slots=True)
class AnnotationAdapter:
    """Compile once, validate isolated JSON values, and serialize with explicit policy.

    Accepted values retain their JSON numeric types. A float annotation denotes
    the JSON number family; accepting integer JSON under it does not coerce it.
    This adapter does not instantiate dataclasses or invoke user constructors.
    """

    annotation: object = field(repr=False)
    limits: OutputLimits = field(default_factory=OutputLimits)
    definition_limits: SchemaDefinitionLimits = field(default_factory=SchemaDefinitionLimits)
    serialization: JSONSerializationOptions = field(default_factory=JSONSerializationOptions)
    schema: OutputSchema = field(init=False)

    def __post_init__(self) -> None:
        if (
            type(self.limits) is not OutputLimits
            or type(self.serialization) is not JSONSerializationOptions
            or type(self.definition_limits) is not SchemaDefinitionLimits
        ):
            raise ValueError("limits and serialization must have their declared types")
        object.__setattr__(
            self, "schema", schema_for_annotation(self.annotation, limits=self.definition_limits)
        )

    def validate_python(self, value: object) -> JSONValue:
        document = snapshot_json(value, self.limits)
        issues = self.schema.validate(document, limits=self.limits)
        if issues:
            raise PayloadValidationError(issues)
        return document

    def validate_json_bytes(
        self, payload: bytes | bytearray | memoryview, *, max_input_bytes: int = 4_000_000
    ) -> JSONValue:
        return self.validate_python(decode_json_bytes(payload, max_input_bytes=max_input_bytes))

    def dump_json(self, value: object) -> bytes:
        document = self.validate_python(value)
        return _encode_json(document, self.serialization)


def _encode_json(document: JSONValue, options: JSONSerializationOptions) -> bytes:
    """Shared bounded serializer for already validated, isolated JSON snapshots."""
    encoder = json.JSONEncoder(
        ensure_ascii=options.ensure_ascii,
        sort_keys=options.sort_keys,
        indent=options.indent,
        separators=(",", ":") if options.indent is None else None,
        allow_nan=False,
    )
    chunks: list[bytes] = []
    size = 0
    for chunk in encoder.iterencode(document):
        if len(chunk) > options.max_output_bytes - size:
            raise OutputContractError("serialized JSON byte limit exceeded")
        size += sum(
            1 + (ord(character) > 0x7F) + (ord(character) > 0x7FF) + (ord(character) > 0xFFFF)
            for character in chunk
        )
        if size > options.max_output_bytes:
            raise OutputContractError("serialized JSON byte limit exceeded")
        chunks.append(chunk.encode("utf-8"))
    return b"".join(chunks)
