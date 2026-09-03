"""Strict, bounded adapters for raw HTTP request bodies.

The functions in this module stop at the application boundary: they decode a
body that an HTTP server has already bounded and read.  They deliberately do
not start a server, inspect headers, decompress transfer encodings, or trust a
``Content-Length`` value.
"""

from __future__ import annotations

import json
import math
from typing import Any

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.models import Manifest
from payload_palette.normalizer import normalize
from payload_palette.policy import NormalizationPolicy

DEFAULT_MAX_INPUT_BYTES = 192 * 1024 * 1024
MAX_INGRESS_INPUT_BYTES = 512 * 1024 * 1024
MAX_JSON_DEPTH = 128
MAX_JSON_INTEGER_DIGITS = 256


def decode_json_bytes(
    payload: bytes | bytearray | memoryview,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> Any:
    """Decode one already-read request body using the strict CLI JSON contract.

    The byte limit is checked before copying or UTF-8 decoding.  Framework
    integrations must also configure an equivalent or smaller server-side body
    limit so an oversized request is rejected before it is buffered in memory.
    """

    maximum = validate_input_limit(max_input_bytes)
    try:
        view = memoryview(payload)
    except (TypeError, ValueError) as exc:
        raise PayloadValidationError(
            [ValidationIssue("input_type", "request body must be bytes-like", "$")]
        ) from exc
    try:
        if view.ndim != 1 or view.itemsize != 1:
            raise PayloadValidationError(
                [ValidationIssue("input_type", "request body must be a byte sequence", "$")]
            )
        if view.nbytes > maximum:
            raise _too_large(maximum)
        try:
            text = view.tobytes().decode("utf-8")
        except UnicodeError as exc:
            raise PayloadValidationError(
                [ValidationIssue("input_encoding", f"input is not valid UTF-8: {exc}", "$")]
            ) from exc
    finally:
        view.release()
    return _decode_json_text(text)


def normalize_json_bytes(
    payload: bytes | bytearray | memoryview,
    policy: NormalizationPolicy | None = None,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> Manifest:
    """Strictly decode and normalize one buffered HTTP request body."""

    if policy is not None and not isinstance(policy, NormalizationPolicy):
        raise ValueError("policy must be a NormalizationPolicy or None")
    active_policy = NormalizationPolicy() if policy is None else policy
    return normalize(
        decode_json_bytes(payload, max_input_bytes=max_input_bytes),
        active_policy,
    )


def validate_input_limit(value: object) -> int:
    """Validate a configurable ingress body limit without accepting booleans."""

    if type(value) is not int or not 1 <= value <= MAX_INGRESS_INPUT_BYTES:
        raise ValueError(f"max_input_bytes must be between 1 and {MAX_INGRESS_INPUT_BYTES}")
    return value


def _decode_json_text(text: str) -> Any:
    _check_json_depth(text)
    try:
        document = json.loads(
            text,
            object_pairs_hook=_unique_json_object,
            parse_constant=_reject_json_constant,
            parse_float=_finite_json_float,
            parse_int=_bounded_json_int,
        )
    except PayloadValidationError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
        location = (
            f" at line {exc.lineno}, column {exc.colno}"
            if isinstance(exc, json.JSONDecodeError)
            else ""
        )
        message = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise PayloadValidationError(
            [ValidationIssue("invalid_json", f"invalid JSON{location}: {message}", "$")]
        ) from exc
    _validate_json_unicode(document)
    return document


def _too_large(maximum: int, path: str = "$") -> PayloadValidationError:
    return PayloadValidationError(
        [
            ValidationIssue(
                "input_too_large",
                f"JSON input exceeds the {maximum}-byte limit",
                path,
            )
        ]
    )


def _check_json_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise PayloadValidationError(
                    [
                        ValidationIssue(
                            "json_too_deep",
                            f"JSON nesting exceeds the {MAX_JSON_DEPTH}-level limit",
                            "$",
                        )
                    ]
                )
        elif character in "]}":
            depth = max(depth - 1, 0)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PayloadValidationError(
                [
                    ValidationIssue(
                        "duplicate_json_key",
                        f"JSON object contains duplicate key {key!r}",
                        "$",
                    )
                ]
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise PayloadValidationError(
        [
            ValidationIssue(
                "nonstandard_json_number",
                f"JSON contains non-standard numeric constant {value!r}",
                "$",
            )
        ]
    )


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise PayloadValidationError(
            [ValidationIssue("json_number_range", "JSON number is outside the finite range", "$")]
        )
    return parsed


def _bounded_json_int(value: str) -> int:
    if len(value.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
        raise PayloadValidationError(
            [
                ValidationIssue(
                    "json_number_range",
                    f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits",
                    "$",
                )
            ]
        )
    return int(value)


def _validate_json_unicode(document: Any) -> None:
    if isinstance(document, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in document):
            raise PayloadValidationError(
                [
                    ValidationIssue(
                        "invalid_unicode",
                        "JSON contains an isolated Unicode surrogate code point",
                        "$",
                    )
                ]
            )
    elif isinstance(document, list):
        for value in document:
            _validate_json_unicode(value)
    elif isinstance(document, dict):
        for key, value in document.items():
            _validate_json_unicode(key)
            _validate_json_unicode(value)
