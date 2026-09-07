"""Strict runtime import of the supported JSON Schema keyword subset."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.ingress import decode_json_bytes
from payload_palette.output_schema import (
    JSONScalar,
    JSONValue,
    OutputPath,
    OutputSchema,
    SchemaKind,
    _number,
    _text,
    output_path,
)

SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
_KINDS = ("null", "boolean", "integer", "number", "string", "array", "object")


@dataclass(frozen=True, slots=True)
class SchemaDefinitionLimits:
    max_depth: int = 32
    max_nodes: int = 4_096
    max_characters: int = 1_000_000
    max_branches: int = 16

    def __post_init__(self) -> None:
        for name, minimum, ceiling in (
            ("max_depth", 0, 32),
            ("max_nodes", 1, 4_096),
            ("max_characters", 1, 1_000_000),
            ("max_branches", 2, 16),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= ceiling:
                raise ValueError(f"{name} must be an integer between {minimum} and {ceiling}")


class SchemaDefinitionError(PayloadValidationError):
    """An unsupported, malformed or oversized runtime schema definition."""


def _definition_error(code: str, message: str, path: OutputPath) -> SchemaDefinitionError:
    return SchemaDefinitionError([ValidationIssue(code, message, output_path(path))])


def _definition_limits(limits: SchemaDefinitionLimits | None) -> SchemaDefinitionLimits:
    if limits is not None and type(limits) is not SchemaDefinitionLimits:
        raise ValueError("limits must be SchemaDefinitionLimits or None")
    return SchemaDefinitionLimits() if limits is None else limits


def load_output_schema(
    document: object, *, limits: SchemaDefinitionLimits | None = None
) -> OutputSchema:
    """Compile strict schema data without reference resolution, coercion or imports.

    Imported objects follow JSON Schema's default-open additionalProperties
    semantics. OutputSchema constructors remain default-closed, and export always
    writes the choice explicitly. Unknown or inapplicable keywords are errors.
    """

    active_limits = _definition_limits(limits)
    active: set[int] = set()
    nodes = 0
    characters = 0

    def charge(text: str, path: OutputPath) -> None:
        nonlocal characters
        characters += len(text)
        if characters > active_limits.max_characters:
            raise _definition_error(
                "schema_definition_budget", "schema character budget exceeded", path
            )

    def require_keys(node: dict[str, object], allowed: set[str], path: OutputPath) -> None:
        if len(node) > len(allowed):
            raise _definition_error("schema_definition_keyword", "unsupported schema keyword", path)
        for key in node:
            if type(key) is not str or len(key) > 64 or key not in allowed:
                raise _definition_error(
                    "schema_definition_keyword", "unsupported schema keyword", path
                )

    def build(raw: object, depth: int, path: OutputPath) -> OutputSchema:
        nonlocal nodes
        nodes += 1
        if depth > active_limits.max_depth or nodes > active_limits.max_nodes:
            raise _definition_error(
                "schema_definition_budget", "schema depth or node budget exceeded", path
            )
        if type(raw) is not dict:
            raise _definition_error(
                "schema_definition_type", "schema nodes must be built-in objects", path
            )
        if id(raw) in active:
            raise _definition_error("schema_definition_cycle", "cyclic schema definition", path)
        node = cast(dict[str, object], raw)
        if len(node) > 8:
            raise _definition_error("schema_definition_keyword", "too many schema keywords", path)
        if any(type(key) is not str or len(key) > 64 for key in node):
            raise _definition_error(
                "schema_definition_keyword", "schema keyword names must be bounded strings", path
            )
        active.add(id(raw))
        try:
            if "$schema" in node and (
                path or type(node["$schema"]) is not str or node["$schema"] != SCHEMA_DIALECT
            ):
                raise _definition_error(
                    "schema_definition_dialect", "unsupported or nested schema dialect", path
                )
            if "anyOf" in node:
                require_keys(node, {"$schema", "anyOf"}, path)
                branches = node["anyOf"]
                if (
                    type(branches) is not list
                    or not 2 <= len(branches) <= active_limits.max_branches
                ):
                    raise _definition_error(
                        "schema_definition_branches",
                        "anyOf requires a bounded list of 2..16 schemas",
                        path,
                    )
                return OutputSchema(
                    "union",
                    any_of=tuple(
                        build(branch, depth + 1, (*path, "anyOf", index))
                        for index, branch in enumerate(branches)
                    ),
                )
            kind = node.get("type")
            if type(kind) is list:
                require_keys(node, {"$schema", "type"}, path)
                if (
                    not 1 <= len(kind) <= min(len(_KINDS), active_limits.max_branches)
                    or any(type(item) is not str or item not in _KINDS for item in kind)
                    or len(set(kind)) != len(kind)
                ):
                    raise _definition_error(
                        "schema_definition_branches",
                        "type list must contain unique supported JSON types",
                        path,
                    )
                branches = tuple(
                    build({"type": item}, depth + 1, (*path, "type", index))
                    for index, item in enumerate(kind)
                )
                return branches[0] if len(branches) == 1 else OutputSchema("union", any_of=branches)
            if type(kind) is not str or kind not in _KINDS:
                raise _definition_error(
                    "schema_definition_type",
                    "an explicit supported type or anyOf is required",
                    path,
                )
            allowed = {"$schema", "type", "enum", "const"}
            if kind in ("integer", "number"):
                allowed |= {"minimum", "maximum"}
            if kind == "integer":
                allowed.add("x-payload-strict-integer")
            if kind == "string":
                allowed |= {"minLength", "maxLength"}
            elif kind == "array":
                allowed |= {"items", "minItems", "maxItems"}
            elif kind == "object":
                allowed |= {"properties", "required", "additionalProperties"}
            require_keys(node, allowed, path)
            if "x-payload-strict-integer" in node and node["x-payload-strict-integer"] is not True:
                raise _definition_error(
                    "schema_definition_constraint", "strict integer extension must be true", path
                )
            for keyword in ("minimum", "maximum"):
                if keyword in node and not _number(node[keyword]):
                    raise _definition_error(
                        "schema_definition_constraint",
                        "numeric constraints require finite numbers",
                        (*path, keyword),
                    )
            for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
                if keyword in node and type(node[keyword]) is not int:
                    raise _definition_error(
                        "schema_definition_constraint",
                        "length constraints require integers",
                        (*path, keyword),
                    )
            if "enum" in node and "const" in node:
                raise _definition_error(
                    "schema_definition_constraint", "use enum or const, not both", path
                )
            enum: tuple[JSONScalar, ...] | None = None
            if "enum" in node or "const" in node:
                values = [node["const"]] if "const" in node else node["enum"]
                if (
                    type(values) is not list
                    or not 1 <= len(values) <= 256
                    or any(
                        type(value) not in (str, int, float, bool, type(None)) for value in values
                    )
                ):
                    raise _definition_error(
                        "schema_definition_constraint",
                        "enum/const must contain bounded scalar values",
                        path,
                    )
                for value in values:
                    if type(value) is str:
                        if len(value) > 2_048 or not _text(value):
                            raise _definition_error(
                                "schema_definition_constraint",
                                "enum strings must be bounded Unicode",
                                path,
                            )
                        charge(value, path)
                    elif type(value) in (int, float) and not _number(value):
                        raise _definition_error(
                            "schema_definition_constraint",
                            "enum numbers must be bounded and finite",
                            path,
                        )
                for index, value in enumerate(values):
                    if any(
                        (
                            type(value) is type(previous)
                            or (type(value) in (int, float) and type(previous) in (int, float))
                        )
                        and value == previous
                        for previous in values[:index]
                    ):
                        raise _definition_error(
                            "schema_definition_constraint", "enum members must be unique", path
                        )
                enum = cast(tuple[JSONScalar, ...], tuple(values))
            properties: dict[str, OutputSchema] = {}
            required: tuple[str, ...] = ()
            additional: bool | OutputSchema = False
            items: OutputSchema | None = None
            if kind == "object":
                raw_properties = node.get("properties", {})
                if type(raw_properties) is not dict or len(raw_properties) > 256:
                    raise _definition_error(
                        "schema_definition_type",
                        "properties must be an object with at most 256 members",
                        path,
                    )
                for key, value in raw_properties.items():
                    if type(key) is not str or len(key) > 256 or not _text(key):
                        raise _definition_error(
                            "schema_definition_type",
                            "property names must be bounded Unicode strings",
                            path,
                        )
                    charge(key, path)
                    properties[key] = build(value, depth + 1, (*path, "properties", key))
                raw_required = node.get("required", [])
                if (
                    type(raw_required) is not list
                    or len(raw_required) > 256
                    or any(
                        type(key) is not str or len(key) > 256 or not _text(key)
                        for key in raw_required
                    )
                ):
                    raise _definition_error(
                        "schema_definition_type", "required must be a bounded string list", path
                    )
                required = tuple(raw_required)
                raw_additional = node.get("additionalProperties", True)
                if type(raw_additional) is bool:
                    additional = raw_additional
                elif type(raw_additional) is dict:
                    additional = build(raw_additional, depth + 1, (*path, "additionalProperties"))
                else:
                    raise _definition_error(
                        "schema_definition_type",
                        "additionalProperties must be boolean or a schema",
                        path,
                    )
            elif kind == "array":
                if "items" not in node:
                    raise _definition_error(
                        "schema_definition_type", "array schemas require items", path
                    )
                items = build(node["items"], depth + 1, (*path, "items"))
            suffix = "Items" if kind == "array" else "Length"
            try:
                return OutputSchema(
                    cast(SchemaKind, kind),
                    properties=properties,
                    required=required,
                    items=items,
                    additional_properties=additional,
                    minimum=cast(int | float | None, node.get("minimum")),
                    maximum=cast(int | float | None, node.get("maximum")),
                    min_length=cast(int | None, node.get(f"min{suffix}")),
                    max_length=cast(int | None, node.get(f"max{suffix}")),
                    enum=enum,
                    integer_mode="json"
                    if kind == "integer" and "x-payload-strict-integer" not in node
                    else "strict",
                )
            except ValueError as exc:
                raise _definition_error(
                    "schema_definition_constraint",
                    "invalid or incompatible schema constraints",
                    path,
                ) from exc
        except SchemaDefinitionError:
            raise
        except ValueError as exc:
            raise _definition_error(
                "schema_definition_constraint", "invalid or oversized schema definition", path
            ) from exc
        finally:
            active.remove(id(raw))

    return build(document, 0, ())


def load_output_schema_json_bytes(
    payload: bytes | bytearray | memoryview,
    *,
    max_input_bytes: int = 4_000_000,
    limits: SchemaDefinitionLimits | None = None,
) -> OutputSchema:
    """Use strict bounded JSON decoding before compiling a schema definition."""

    return load_output_schema(
        decode_json_bytes(payload, max_input_bytes=max_input_bytes), limits=limits
    )


def export_output_schema(
    schema: OutputSchema, *, include_dialect: bool = True
) -> dict[str, JSONValue]:
    """Return a fresh normalized supported-keyword document."""

    if type(schema) is not OutputSchema or type(include_dialect) is not bool:
        raise ValueError("schema and include_dialect must have their declared types")
    document = schema.json_schema(preserve_python_types=True)
    return {"$schema": SCHEMA_DIALECT, **document} if include_dialect else document
