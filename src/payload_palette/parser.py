"""Parse common ordered multimodal content arrays into a small internal contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard, cast

from payload_palette.errors import PayloadValidationError, ValidationIssue, problem
from payload_palette.media import mime_from_format, normalize_mime_type
from payload_palette.models import (
    EnvelopeName,
    LargeValue,
    PartKind,
    PartSpec,
    SourceKind,
    value_prefix,
)

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

# Envelopes that declare a MIME type instead of a part type carry their kind in
# the MIME major type.  Anything outside this table is refused rather than
# guessed, so a document or function-call payload cannot enter the pipeline.
_MIME_MAJOR_KINDS: dict[str, PartKind] = {
    "image": "image",
    "audio": "audio",
    "video": "video",
}

_GEMINI_PART_FIELDS = ("text", "inline_data", "file_data")

_AppendEntry = Callable[[Any, str], None]
_AppendIssue = Callable[[ValidationIssue], None]
_Adapter = Callable[[Any, _AppendEntry, _AppendIssue], None]


@dataclass(frozen=True, slots=True)
class _AdaptedPart:
    """A part already resolved to its kind, value, and exact source value path.

    Envelope adapters translate shape only.  The resulting :class:`PartSpec`
    re-enters the same normalization pipeline as a natively shaped part, so
    every size, MIME, signature, and remote-URL rule applies unchanged.
    """

    kind: PartKind
    value: str | LargeValue
    value_path: str
    source_hint: SourceKind
    declared_mime_type: str | None = None


def _is_content(value: Any) -> TypeGuard[str | LargeValue]:
    """Report a JSON string value, whether or not the stream kept its characters.

    A streamed request replaces an over-long string with a measurement, and the
    envelope adapters care only that a *string* occupied the position.  Whoever
    consumes the value decides whether the missing characters matter: text and
    URL parts refuse it on length alone, and inline media never needed them.
    """

    return isinstance(value, (str, LargeValue))


def _is_present_content(value: Any) -> TypeGuard[str | LargeValue]:
    """Report a non-empty JSON string value.

    A streamed value is longer than the threshold that produced it, so it is
    never the empty string these checks exist to catch.
    """

    return isinstance(value, LargeValue) or (isinstance(value, str) and bool(value))


def _reject_surrogates(value: str | LargeValue, path: str) -> None:
    """Scan a materialized value; a streamed one was checked as it decoded."""

    if isinstance(value, str):
        _reject_isolated_surrogates(value, path)


def _adapted_text(value: str | LargeValue, value_path: str) -> _AdaptedPart:
    return _AdaptedPart(
        kind="text",
        value=value,
        value_path=value_path,
        source_hint="text",
        declared_mime_type="text/plain",
    )


def _is_array(value: Any) -> TypeGuard[Sequence[Any]]:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


def extract_entries(
    document: Any,
    max_parts: int | None = None,
    *,
    envelope: EnvelopeName = "default",
) -> list[tuple[Any, str]]:
    """Locate content parts in the named request envelope.

    The caller always names ``envelope``.  Vendor shapes are never detected from
    the document itself, because guessing would weaken the deliberate
    ``ambiguous_envelope`` rejection that the default contract relies on.
    """

    adapter = _ENVELOPE_ADAPTERS.get(envelope)
    if adapter is None:
        raise ValueError(f"unsupported envelope: {envelope!r}")
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

    adapter(document, append_entry, append_issue)
    if issues:
        raise PayloadValidationError(issues)
    if not entries:
        raise problem("empty_content", "payload does not contain any content parts", "$")
    return entries


def _extract_default(document: Any, append_entry: _AppendEntry, append_issue: _AppendIssue) -> None:
    """Read a direct array, a ``content`` envelope, ``messages``, or one typed part."""

    if _is_array(document):
        for index, part in enumerate(document):
            append_entry(part, f"$[{index}]")
        return
    if not isinstance(document, Mapping):
        raise problem("payload_type", "payload root must be an object or array", "$")
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
                    ValidationIssue("message_type", "each message must be an object", message_path)
                )
                continue
            content = message.get("content")
            content_path = f"{message_path}.content"
            if _is_content(content):
                append_entry(_adapted_text(content, content_path), content_path)
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


def _envelope_array(document: Any, key: str) -> Sequence[Any]:
    """Return the required top-level array of a named vendor envelope."""

    if not isinstance(document, Mapping):
        raise problem("payload_type", "payload root must be an object", "$")
    if key not in document:
        raise problem("payload_shape", f"payload must contain a {key} array", "$")
    value = document[key]
    if not _is_array(value):
        raise problem(f"{key}_type", f"{key} must be an array", f"$.{key}")
    return value


def _typed_block(
    block: Any, path: str, append_issue: _AppendIssue
) -> tuple[Mapping[str, Any], str] | None:
    """Return one object block and its normalized ``type``, or report why not."""

    if not isinstance(block, Mapping):
        append_issue(ValidationIssue("part_type", "each content block must be an object", path))
        return None
    raw_type = block.get("type")
    if not isinstance(raw_type, str):
        append_issue(
            ValidationIssue("missing_type", "content block requires a string type", f"{path}.type")
        )
        return None
    return cast(Mapping[str, Any], block), raw_type.strip().lower()


def _adapted_mime(value: str, path: str, append_issue: _AppendIssue) -> str | None:
    """Normalize a vendor MIME declaration, reporting failures at its own path."""

    try:
        return normalize_mime_type(value, path)
    except PayloadValidationError as exc:
        for issue in exc.issues:
            append_issue(issue)
        return None


def _extract_anthropic(
    document: Any, append_entry: _AppendEntry, append_issue: _AppendIssue
) -> None:
    """Read an Anthropic Messages request: ``messages[].content`` text and image blocks."""

    for message_index, message in enumerate(_envelope_array(document, "messages")):
        message_path = f"$.messages[{message_index}]"
        if not isinstance(message, Mapping):
            append_issue(
                ValidationIssue("message_type", "each message must be an object", message_path)
            )
            continue
        content = message.get("content")
        content_path = f"{message_path}.content"
        if _is_content(content):
            append_entry(_adapted_text(content, content_path), content_path)
            continue
        if not _is_array(content):
            append_issue(
                ValidationIssue(
                    "content_type",
                    "message content must be a string or an array",
                    content_path,
                )
            )
            continue
        for index, block in enumerate(content):
            _anthropic_block(block, f"{content_path}[{index}]", append_entry, append_issue)


def _anthropic_block(
    block: Any, path: str, append_entry: _AppendEntry, append_issue: _AppendIssue
) -> None:
    typed = _typed_block(block, path, append_issue)
    if typed is None:
        return
    content_block, block_type = typed
    if block_type == "text":
        value = content_block.get("text")
        if not _is_content(value):
            append_issue(
                ValidationIssue(
                    "text_type", "text block requires a string text field", f"{path}.text"
                )
            )
            return
        append_entry(_adapted_text(value, f"{path}.text"), path)
        return
    if block_type != "image":
        append_issue(
            ValidationIssue(
                "unknown_type",
                f"unsupported content block type {block_type!r}",
                f"{path}.type",
            )
        )
        return
    _anthropic_image(content_block, path, append_entry, append_issue)


def _anthropic_image(
    block: Mapping[str, Any], path: str, append_entry: _AppendEntry, append_issue: _AppendIssue
) -> None:
    """Translate the ``source`` object of an Anthropic image block."""

    source = block.get("source")
    source_path = f"{path}.source"
    if not isinstance(source, Mapping):
        append_issue(
            ValidationIssue("media_container", "image source must be an object", source_path)
        )
        return
    source_type = source.get("type")
    if not isinstance(source_type, str):
        append_issue(
            ValidationIssue(
                "missing_type", "image source requires a string type", f"{source_path}.type"
            )
        )
        return
    normalized_type = source_type.strip().lower()
    if normalized_type == "base64":
        _anthropic_base64_source(source, path, source_path, append_entry, append_issue)
        return
    if normalized_type == "url":
        url = source.get("url")
        if not _is_present_content(url):
            append_issue(
                ValidationIssue(
                    "missing_media",
                    "image source requires a non-empty url value",
                    f"{source_path}.url",
                )
            )
            return
        append_entry(
            _AdaptedPart(
                kind="image", value=url, value_path=f"{source_path}.url", source_hint="remote"
            ),
            path,
        )
        return
    append_issue(
        ValidationIssue(
            "unsupported_source",
            f"image source type {source_type!r} cannot be resolved without fetching",
            f"{source_path}.type",
            "send the image as a base64 or url source",
        )
    )


def _anthropic_base64_source(
    source: Mapping[str, Any],
    path: str,
    source_path: str,
    append_entry: _AppendEntry,
    append_issue: _AppendIssue,
) -> None:
    media_type = source.get("media_type")
    if not isinstance(media_type, str):
        append_issue(
            ValidationIssue(
                "mime_type_field", "media_type must be a string", f"{source_path}.media_type"
            )
        )
        return
    mime_type = _adapted_mime(media_type, f"{source_path}.media_type", append_issue)
    if mime_type is None:
        return
    data = source.get("data")
    if not _is_present_content(data):
        append_issue(
            ValidationIssue(
                "missing_media",
                "image source requires a non-empty data value",
                f"{source_path}.data",
            )
        )
        return
    append_entry(
        _AdaptedPart(
            kind="image",
            value=data,
            value_path=f"{source_path}.data",
            source_hint="inline",
            declared_mime_type=mime_type,
        ),
        path,
    )


def _extract_gemini(document: Any, append_entry: _AppendEntry, append_issue: _AppendIssue) -> None:
    """Read a Google Gemini ``generateContent`` request: ``contents[].parts[]``."""

    for content_index, content in enumerate(_envelope_array(document, "contents")):
        content_path = f"$.contents[{content_index}]"
        if not isinstance(content, Mapping):
            append_issue(
                ValidationIssue(
                    "message_type", "each content entry must be an object", content_path
                )
            )
            continue
        parts = content.get("parts")
        parts_path = f"{content_path}.parts"
        if not _is_array(parts):
            append_issue(ValidationIssue("content_type", "parts must be an array", parts_path))
            continue
        for index, part in enumerate(parts):
            _gemini_part(part, f"{parts_path}[{index}]", append_entry, append_issue)


def _gemini_part(
    part: Any, path: str, append_entry: _AppendEntry, append_issue: _AppendIssue
) -> None:
    if not isinstance(part, Mapping):
        append_issue(ValidationIssue("part_type", "each part must be an object", path))
        return
    present = [key for key in _GEMINI_PART_FIELDS if key in part]
    if len(present) > 1:
        append_issue(
            ValidationIssue(
                "ambiguous_media",
                f"part contains multiple values: {', '.join(present)}",
                path,
            )
        )
        return
    if not present:
        append_issue(
            ValidationIssue(
                "unsupported_part",
                "part contains no text, inline_data, or file_data field",
                path,
            )
        )
        return
    field = present[0]
    if field == "text":
        value = part["text"]
        if not _is_content(value):
            append_issue(
                ValidationIssue(
                    "text_type", "text part requires a string text field", f"{path}.text"
                )
            )
            return
        append_entry(_adapted_text(value, f"{path}.text"), path)
        return
    _gemini_media(part[field], field, path, append_entry, append_issue)


def _gemini_media(
    container: Any,
    field: str,
    path: str,
    append_entry: _AppendEntry,
    append_issue: _AppendIssue,
) -> None:
    """Translate a Gemini ``inline_data`` blob or ``file_data`` reference."""

    container_path = f"{path}.{field}"
    if not isinstance(container, Mapping):
        append_issue(
            ValidationIssue("media_container", f"{field} must be an object", container_path)
        )
        return
    declared = container.get("mime_type")
    if not isinstance(declared, str):
        append_issue(
            ValidationIssue(
                "mime_type_field",
                "mime_type must be a string",
                f"{container_path}.mime_type",
            )
        )
        return
    mime_type = _adapted_mime(declared, f"{container_path}.mime_type", append_issue)
    if mime_type is None:
        return
    kind = _MIME_MAJOR_KINDS.get(mime_type.split("/", 1)[0])
    if kind is None:
        append_issue(
            ValidationIssue(
                "unsupported_media_kind",
                f"MIME type {mime_type!r} is not an image, audio, or video part",
                f"{container_path}.mime_type",
            )
        )
        return
    inline = field == "inline_data"
    value_key = "data" if inline else "file_uri"
    value = container.get(value_key)
    if not _is_present_content(value):
        append_issue(
            ValidationIssue(
                "missing_media",
                f"{field} requires a non-empty {value_key} value",
                f"{container_path}.{value_key}",
            )
        )
        return
    append_entry(
        _AdaptedPart(
            kind=kind,
            value=value,
            value_path=f"{container_path}.{value_key}",
            source_hint="inline" if inline else "remote",
            declared_mime_type=mime_type,
        ),
        path,
    )


def _extract_ollama(document: Any, append_entry: _AppendEntry, append_issue: _AppendIssue) -> None:
    """Read an Ollama request: a ``prompt`` or ``messages``, each with sibling images."""

    if not isinstance(document, Mapping):
        raise problem("payload_type", "payload root must be an object", "$")
    roots = [key for key in ("prompt", "messages") if key in document]
    if len(roots) > 1:
        raise problem(
            "ambiguous_envelope",
            f"payload combines incompatible envelope fields: {', '.join(roots)}",
            "$",
        )
    if not roots:
        raise problem(
            "payload_shape",
            "expected a prompt string or a messages array",
            "$",
        )
    if roots[0] == "prompt":
        prompt = document["prompt"]
        if not _is_content(prompt):
            raise problem("text_type", "prompt must be a string", "$.prompt")
        append_entry(_adapted_text(prompt, "$.prompt"), "$.prompt")
        _ollama_images(document, "$", append_entry, append_issue)
        return
    messages = document["messages"]
    if not _is_array(messages):
        raise problem("messages_type", "messages must be an array", "$.messages")
    for index, message in enumerate(messages):
        message_path = f"$.messages[{index}]"
        if not isinstance(message, Mapping):
            append_issue(
                ValidationIssue("message_type", "each message must be an object", message_path)
            )
            continue
        content = message.get("content")
        content_path = f"{message_path}.content"
        if not _is_content(content):
            append_issue(
                ValidationIssue("content_type", "message content must be a string", content_path)
            )
            continue
        append_entry(_adapted_text(content, content_path), content_path)
        _ollama_images(message, message_path, append_entry, append_issue)


def _ollama_images(
    container: Mapping[str, Any],
    path: str,
    append_entry: _AppendEntry,
    append_issue: _AppendIssue,
) -> None:
    """Append the images that accompany one prompt or chat message.

    Ollama associates images with their text positionally instead of
    interleaving them, so the payload expresses no order between the two.  This
    adapter fixes one: the text first, then each entry in ``images`` order.
    Manifest ordinals therefore record that documented rule rather than an
    ordering the request actually made.
    """

    if "images" not in container:
        return
    images = container["images"]
    images_path = f"{path}.images"
    if not _is_array(images):
        append_issue(ValidationIssue("content_type", "images must be an array", images_path))
        return
    for index, image in enumerate(images):
        image_path = f"{images_path}[{index}]"
        if not _is_present_content(image):
            append_issue(
                ValidationIssue(
                    "missing_media",
                    "each image must be a non-empty Base64 string",
                    image_path,
                )
            )
            continue
        append_entry(
            _AdaptedPart(kind="image", value=image, value_path=image_path, source_hint="inline"),
            image_path,
        )


_ENVELOPE_ADAPTERS: dict[str, _Adapter] = {
    "default": _extract_default,
    "anthropic": _extract_anthropic,
    "gemini": _extract_gemini,
    "ollama": _extract_ollama,
}


def parse_document(
    document: Any, max_parts: int, *, envelope: EnvelopeName = "default"
) -> list[PartSpec]:
    """Parse every located entry while aggregating independent shape errors."""

    entries = extract_entries(document, max_parts=max_parts, envelope=envelope)
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

    if isinstance(raw, _AdaptedPart):
        return _adapted_spec(raw, path, ordinal)
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


def _adapted_spec(raw: _AdaptedPart, path: str, ordinal: int) -> PartSpec:
    """Finish an adapter translation under the rules a native part also obeys."""

    if isinstance(raw.value, str):
        _reject_isolated_surrogates(raw.value, raw.value_path)
    # A streamed value needs no surrogate scan here: the decoder refuses an
    # isolated surrogate while the characters go past, so one can never reach
    # this point inside a LargeValue.
    if raw.source_hint == "remote" and not value_prefix(raw.value, 8).lower().startswith(
        ("http://", "https://")
    ):
        raise problem("url_scheme", "media URL must use HTTP or HTTPS", raw.value_path)
    return PartSpec(
        ordinal=ordinal,
        path=path,
        value_path=raw.value_path,
        kind=raw.kind,
        value=raw.value,
        source_hint=raw.source_hint,
        declared_mime_type=raw.declared_mime_type,
    )


def _parse_text(raw: Mapping[str, Any], path: str, ordinal: int) -> PartSpec:
    value = raw.get("text")
    if not _is_content(value):
        raise problem("text_type", "text part requires a string text field", f"{path}.text")
    _reject_surrogates(value, f"{path}.text")
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
        if _is_content(container):
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

    if not _is_present_content(value):
        raise problem(
            "missing_media",
            f"{kind} part requires a non-empty data or URL value",
            value_path,
        )
    _reject_surrogates(value, value_path)
    prefix = value_prefix(value, 8).lower()
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
