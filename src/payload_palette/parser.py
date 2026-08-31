"""Parse common ordered multimodal content arrays into a small internal contract."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from payload_palette.errors import PayloadValidationError, ValidationIssue, problem
from payload_palette.media import mime_from_format, normalize_mime_type
from payload_palette.models import PartKind, PartSpec, SourceKind

_TYPE_ALIASES: dict[str, PartKind] = {
    "text": "text",
    "input_text": "text",
    "image": "image",
    "input_image": "image",
    "image_url": "image",
    "audio": "audio",
    "input_audio": "audio",
    "audio_url": "audio",
    "video": "video",
    "input_video": "video",
    "video_url": "video",
}


@dataclass(frozen=True, slots=True)
class _DirectMessageText:
    """A message content string whose value path is the content path itself."""

    value: str


def _is_array(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def extract_entries(document: Any, max_parts: int | None = None) -> list[tuple[Any, str]]:
    """Locate content parts in a direct array, envelope, or messages array."""

    entries: list[tuple[Any, str]] = []
    issues: list[ValidationIssue] = []

    def append_entry(part: Any, path: str) -> None:
        if max_parts is not None and len(entries) >= max_parts:
            raise problem(
                "too_many_parts",
                f"payload contains more than {max_parts} parts; limit is {max_parts}",
                "$",
            )
        entries.append((part, path))

    def append_issue(issue: ValidationIssue) -> None:
        if max_parts is not None and len(issues) >= max_parts:
            raise problem(
                "too_many_issues",
                f"payload contains more than {max_parts} structural errors",
                "$",
            )
        issues.append(issue)

    if _is_array(document):
        for index, part in enumerate(document):
            append_entry(part, f"$[{index}]")
    elif isinstance(document, Mapping):
        envelope_fields = [key for key in ("messages", "content", "type") if key in document]
        if len(envelope_fields) > 1:
            raise problem(
                "ambiguous_envelope",
                f"payload combines incompatible envelope fields: {', '.join(envelope_fields)}",
                "$",
            )
        if "messages" in document:
            messages = document["messages"]
            if not _is_array(messages):
                raise problem("messages_type", "messages must be an array", "$.messages")
            for message_index, message in enumerate(messages):
                message_path = f"$.messages[{message_index}]"
                if not isinstance(message, Mapping):
                    append_issue(
                        ValidationIssue(
                            "message_type", "each message must be an object", message_path
                        )
                    )
                    continue
                content = message.get("content")
                content_path = f"{message_path}.content"
                if isinstance(content, str):
                    append_entry(_DirectMessageText(content), content_path)
                elif _is_array(content):
                    for index, part in enumerate(content):
                        append_entry(part, f"{content_path}[{index}]")
                else:
                    append_issue(
                        ValidationIssue(
                            "content_type",
                            "message content must be a string or an array",
                            content_path,
                        )
                    )
        elif "content" in document:
            content = document["content"]
            if not _is_array(content):
                raise problem("content_type", "content must be an array", "$.content")
            for index, part in enumerate(content):
                append_entry(part, f"$.content[{index}]")
        elif "type" in document:
            append_entry(document, "$")
        else:
            raise problem(
                "payload_shape",
                "expected a content array, a messages array, or a single typed part",
                "$",
            )
    else:
        raise problem("payload_type", "payload root must be an object or array", "$")
    if issues:
        raise PayloadValidationError(issues)
    if not entries:
        raise problem("empty_content", "payload does not contain any content parts", "$")
    return entries


def parse_document(document: Any, max_parts: int) -> list[PartSpec]:
    """Parse every located entry while aggregating independent shape errors."""

    entries = extract_entries(document, max_parts=max_parts)
    parsed: list[PartSpec] = []
    issues: list[ValidationIssue] = []
    for ordinal, (raw, path) in enumerate(entries):
        try:
            parsed.append(parse_part(raw, path, ordinal))
        except PayloadValidationError as exc:
            issues.extend(exc.issues)
    if issues:
        raise PayloadValidationError(issues)
    return parsed


def parse_part(raw: Any, path: str, ordinal: int) -> PartSpec:
    """Convert one external part representation into a :class:`PartSpec`."""

    if isinstance(raw, _DirectMessageText):
        _reject_isolated_surrogates(raw.value, path)
        return PartSpec(
            ordinal=ordinal,
            path=path,
            value_path=path,
            kind="text",
            value=raw.value,
            source_hint="text",
            declared_mime_type="text/plain",
        )
    if not isinstance(raw, Mapping):
        raise problem("part_type", "each content part must be an object", path)
    raw_type = raw.get("type")
    if not isinstance(raw_type, str):
        raise problem("missing_type", "content part requires a string type", f"{path}.type")
    normalized_type = raw_type.strip().lower()
    try:
        kind = _TYPE_ALIASES[normalized_type]
    except KeyError as exc:
        raise problem(
            "unknown_type", f"unsupported content part type {raw_type!r}", f"{path}.type"
        ) from exc
    if kind == "text":
        return _parse_text(raw, path, ordinal)
    return _parse_media(cast(Mapping[str, Any], raw), path, ordinal, kind, normalized_type)


def _parse_text(raw: Mapping[str, Any], path: str, ordinal: int) -> PartSpec:
    value = raw.get("text")
    if not isinstance(value, str):
        raise problem("text_type", "text part requires a string text field", f"{path}.text")
    _reject_isolated_surrogates(value, f"{path}.text")
    return PartSpec(
        ordinal=ordinal,
        path=path,
        value_path=f"{path}.text",
        kind="text",
        value=value,
        source_hint="text",
        declared_mime_type="text/plain",
    )


def _parse_media(
    raw: Mapping[str, Any],
    path: str,
    ordinal: int,
    kind: PartKind,
    raw_type: str,
) -> PartSpec:
    declared_mime = _optional_string(raw, ("mime_type", "media_type"), path)
    attributes = _attributes(raw, path)
    value: Any = None
    value_path = path
    source_hint: SourceKind = "inline"

    container_key = f"{kind}_url" if raw_type.endswith("_url") else None
    if raw_type == "input_audio":
        container_key = "input_audio"
    elif raw_type == f"input_{kind}" and f"{kind}_url" in raw:
        container_key = f"{kind}_url"
    if container_key is not None:
        alternate_keys = [key for key in ("data", "url", f"{kind}_base64") if key in raw]
        if alternate_keys:
            raise problem(
                "ambiguous_media",
                f"{container_key} cannot be combined with {', '.join(alternate_keys)}",
                path,
            )
        container = raw.get(container_key)
        value_path = f"{path}.{container_key}"
        if isinstance(container, str):
            value = container
        elif isinstance(container, Mapping):
            present_keys = [key for key in ("data", "url") if key in container]
            if len(present_keys) > 1:
                raise problem(
                    "ambiguous_media",
                    "media container cannot contain both data and url",
                    value_path,
                )
            key = present_keys[0] if present_keys else "url"
            value = container.get(key)
            value_path = f"{value_path}.{key}"
            nested_mime = _optional_string(
                container, ("mime_type", "media_type"), f"{path}.{container_key}"
            )
            declared_mime = _combine_mime(declared_mime, nested_mime, f"{path}.{container_key}")
            format_value = container.get("format")
            if format_value is not None and not isinstance(format_value, str):
                raise problem(
                    "format_type",
                    "format must be a string",
                    f"{path}.{container_key}.format",
                )
            declared_mime = _combine_mime(
                declared_mime,
                mime_from_format(format_value, kind),
                f"{path}.{container_key}.format",
            )
        else:
            raise problem(
                "media_container",
                f"{container_key} must be a string or object",
                value_path,
            )
    else:
        present_keys = [key for key in ("data", "url", f"{kind}_base64") if key in raw]
        if len(present_keys) > 1:
            raise problem(
                "ambiguous_media",
                f"media part contains multiple values: {', '.join(present_keys)}",
                path,
            )
        if present_keys:
            key = present_keys[0]
            value = raw[key]
            value_path = f"{path}.{key}"
        format_value = raw.get("format")
        if format_value is not None and not isinstance(format_value, str):
            raise problem("format_type", "format must be a string", f"{path}.format")
        declared_mime = _combine_mime(
            declared_mime, mime_from_format(format_value, kind), f"{path}.format"
        )

    if not isinstance(value, str) or not value:
        raise problem(
            "missing_media",
            f"{kind} part requires a non-empty data or URL value",
            value_path,
        )
    _reject_isolated_surrogates(value, value_path)
    prefix = value[:8].lower()
    if prefix.startswith("data:"):
        source_hint = "inline"
    elif prefix.startswith(("http://", "https://")):
        source_hint = "remote"
    elif "url" in value_path.rsplit(".", 1)[-1]:
        raise problem("url_scheme", "media URL must use data, HTTP, or HTTPS", value_path)
    elif declared_mime is None:
        raise problem(
            "missing_mime",
            "bare Base64 media requires mime_type, media_type, or a known format",
            value_path,
        )
    return PartSpec(
        ordinal=ordinal,
        path=path,
        value_path=value_path,
        kind=kind,
        value=value,
        source_hint=source_hint,
        declared_mime_type=declared_mime,
        attributes=attributes,
    )


def _optional_string(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> str | None:
    result: str | None = None
    for key in keys:
        if key in raw:
            value = raw[key]
            if not isinstance(value, str):
                raise problem("mime_type_field", f"{key} must be a string", f"{path}.{key}")
            _reject_isolated_surrogates(value, f"{path}.{key}")
            result = _combine_mime(
                result,
                normalize_mime_type(value, f"{path}.{key}"),
                f"{path}.{key}",
            )
    return result


def _combine_mime(existing: str | None, candidate: str | None, path: str) -> str | None:
    if existing is not None and candidate is not None:
        left = existing.split(";", 1)[0].strip().lower()
        right = candidate.split(";", 1)[0].strip().lower()
        if left != right:
            raise problem(
                "mime_conflict",
                f"conflicting MIME declarations {existing!r} and {candidate!r}",
                path,
            )
    return existing or candidate


def _attributes(raw: Mapping[str, Any], path: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    for key in ("detail", "name"):
        value = raw.get(key)
        if isinstance(value, str) and value:
            _reject_isolated_surrogates(value, f"{path}.{key}")
            attributes[key] = value
    return attributes


def _reject_isolated_surrogates(value: str, path: str) -> None:
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise problem(
            "invalid_unicode",
            "string contains an isolated Unicode surrogate code point",
            path,
        )
