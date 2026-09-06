"""Opt-in adapters for Anthropic, Gemini, and Ollama request envelopes."""

from __future__ import annotations

import base64
import json
from io import StringIO
from typing import Any

import pytest

from payload_palette import (
    ENVELOPE_NAMES,
    NormalizationPolicy,
    RemoteURLPolicy,
    normalize,
    validate,
)
from payload_palette.cli import run
from payload_palette.errors import PayloadValidationError
from payload_palette.parser import extract_entries, parse_document

ALLOWED_HOST = RemoteURLPolicy(allowed_hosts=("media.example.org",))
UNRECOGNIZED_BASE64 = base64.b64encode(b"no container signature here").decode()


def policy(envelope: str, **overrides: Any) -> NormalizationPolicy:
    return NormalizationPolicy(envelope=envelope, **overrides)  # type: ignore[arg-type]


def tiny_image_policy(envelope: str) -> NormalizationPolicy:
    """A policy whose image ceiling is smaller than the shared PNG fixture."""

    return policy(
        envelope,
        max_bytes_by_kind={"text": 4096, "image": 8, "audio": 4096, "video": 4096},
    )


def codes(document: Any, envelope: str, **overrides: Any) -> list[str]:
    return [issue.code for issue in validate(document, policy(envelope, **overrides))]


def paths(document: Any, envelope: str, **overrides: Any) -> list[str]:
    return [issue.path for issue in validate(document, policy(envelope, **overrides))]


def anthropic_image(source: Any) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": [{"type": "image", "source": source}]}]}


def gemini_parts(*parts: Any) -> dict[str, Any]:
    return {"contents": [{"role": "user", "parts": list(parts)}]}


# --------------------------------------------------------------------------
# Envelope selection
# --------------------------------------------------------------------------


def test_every_declared_envelope_is_dispatchable() -> None:
    assert ENVELOPE_NAMES == ("default", "anthropic", "gemini", "ollama")
    for name in ENVELOPE_NAMES:
        assert NormalizationPolicy(envelope=name).envelope == name


def test_unknown_envelope_is_rejected_by_the_parser() -> None:
    with pytest.raises(ValueError, match="unsupported envelope"):
        extract_entries([{"type": "text", "text": "a"}], envelope="openai")  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["openai", "", 1, True, None])
def test_policy_rejects_an_unknown_envelope(value: object) -> None:
    with pytest.raises(ValueError, match="envelope must be one of"):
        NormalizationPolicy(envelope=value)  # type: ignore[arg-type]


def test_default_envelope_does_not_understand_a_vendor_shape(png_base64: str) -> None:
    """Adapters are opt-in: the default contract still refuses an Anthropic image."""

    document = anthropic_image({"type": "base64", "media_type": "image/png", "data": png_base64})
    assert codes(document, "default") == ["missing_media"]


def test_a_named_envelope_does_not_accept_the_default_shape() -> None:
    assert codes([{"type": "text", "text": "hello"}], "anthropic") == ["payload_type"]


# --------------------------------------------------------------------------
# Anthropic Messages
# --------------------------------------------------------------------------


def test_anthropic_preserves_block_order(png_base64: str) -> None:
    document = {
        "model": "claude-opus-4",
        "system": "ignored top-level field",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is shown?"},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": png_base64,
                        },
                    },
                ],
            },
            {"role": "assistant", "content": "A logo."},
        ],
    }
    manifest = normalize(document, policy("anthropic"))
    assert [(part.ordinal, part.kind, part.source) for part in manifest.parts] == [
        (0, "text", "text"),
        (1, "image", "inline"),
        (2, "text", "text"),
    ]
    assert [part.path for part in manifest.parts] == [
        "$.messages[0].content[0]",
        "$.messages[0].content[1]",
        "$.messages[1].content",
    ]
    assert manifest.parts[1].mime_type == "image/png"


def test_anthropic_base64_image_matches_the_equivalent_default_payload(png_base64: str) -> None:
    """A shape translation must not change what the manifest records."""

    adapted = normalize(
        anthropic_image({"type": "base64", "media_type": "image/png", "data": png_base64}),
        policy("anthropic"),
    )
    native = normalize([{"type": "image", "data": png_base64, "mime_type": "image/png"}])
    assert adapted.parts[0].fingerprint == native.parts[0].fingerprint
    assert adapted.parts[0].byte_length == native.parts[0].byte_length


def test_anthropic_url_source_is_denied_by_default() -> None:
    document = anthropic_image({"type": "url", "url": "https://media.example.org/a.png"})
    issues = validate(document, policy("anthropic"))
    assert [issue.code for issue in issues] == ["remote_url_disabled"]
    assert issues[0].path == "$.messages[0].content[0].source.url"


def test_anthropic_url_source_uses_the_shared_allowlist() -> None:
    document = anthropic_image({"type": "url", "url": "https://media.example.org/a.png?k=v"})
    manifest = normalize(document, policy("anthropic", remote=ALLOWED_HOST))
    assert manifest.parts[0].source == "remote"
    assert manifest.parts[0].locator == "https://media.example.org/a.png?[redacted]"


def test_anthropic_url_source_rejects_a_non_http_scheme() -> None:
    document = anthropic_image({"type": "url", "url": "ftp://media.example.org/a.png"})
    assert codes(document, "anthropic", remote=ALLOWED_HOST) == ["url_scheme"]


def test_anthropic_file_source_is_refused_with_a_hint() -> None:
    issues = validate(anthropic_image({"type": "file", "file_id": "f1"}), policy("anthropic"))
    assert issues[0].code == "unsupported_source"
    assert issues[0].path == "$.messages[0].content[0].source.type"
    assert issues[0].hint is not None


def test_anthropic_oversized_image_hits_the_existing_cap(png_base64: str) -> None:
    document = anthropic_image({"type": "base64", "media_type": "image/png", "data": png_base64})
    issues = validate(document, tiny_image_policy("anthropic"))
    assert [issue.code for issue in issues] == ["part_too_large"]
    assert issues[0].path == "$.messages[0].content[0].source.data"


def test_anthropic_signature_check_still_applies(wav_base64: str) -> None:
    document = anthropic_image({"type": "base64", "media_type": "image/png", "data": wav_base64})
    assert codes(document, "anthropic") == ["signature_mismatch"]


def test_anthropic_mime_allowlist_still_applies(png_base64: str) -> None:
    document = anthropic_image({"type": "base64", "media_type": "image/tiff", "data": png_base64})
    assert codes(document, "anthropic") == ["mime_not_allowed"]


@pytest.mark.parametrize(
    ("source", "code", "path"),
    [
        ("string", "media_container", "$.messages[0].content[0].source"),
        ({}, "missing_type", "$.messages[0].content[0].source.type"),
        ({"type": "url"}, "missing_media", "$.messages[0].content[0].source.url"),
        (
            {"type": "base64", "media_type": 1},
            "mime_type_field",
            "$.messages[0].content[0].source.media_type",
        ),
        (
            {"type": "base64", "media_type": "image//png", "data": "aGk="},
            "invalid_mime",
            "$.messages[0].content[0].source.media_type",
        ),
        (
            {"type": "base64", "media_type": "image/png", "data": ""},
            "missing_media",
            "$.messages[0].content[0].source.data",
        ),
    ],
)
def test_anthropic_malformed_image_sources(source: Any, code: str, path: str) -> None:
    issues = validate(anthropic_image(source), policy("anthropic"))
    assert [(issue.code, issue.path) for issue in issues] == [(code, path)]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("not-an-object", ["payload_type"]),
        ({"model": "claude"}, ["payload_shape"]),
        ({"messages": {}}, ["messages_type"]),
        ({"messages": []}, ["empty_content"]),
        ({"messages": [7]}, ["message_type"]),
        ({"messages": [{"content": 3}]}, ["content_type"]),
        ({"messages": [{"content": [4]}]}, ["part_type"]),
        ({"messages": [{"content": [{}]}]}, ["missing_type"]),
        ({"messages": [{"content": [{"type": "text"}]}]}, ["text_type"]),
        ({"messages": [{"content": [{"type": "tool_use"}]}]}, ["unknown_type"]),
        ({"messages": [{"content": [{"type": "image"}]}]}, ["media_container"]),
    ],
)
def test_anthropic_malformed_envelopes(document: Any, expected: list[str]) -> None:
    assert codes(document, "anthropic") == expected


def test_anthropic_block_errors_are_aggregated() -> None:
    document = {"messages": [{"content": [{"type": "text"}, {"type": "thinking"}]}]}
    assert codes(document, "anthropic") == ["text_type", "unknown_type"]


def test_anthropic_rejects_an_isolated_surrogate_in_message_text() -> None:
    issues = validate({"messages": [{"content": "\ud800"}]}, policy("anthropic"))
    assert [(issue.code, issue.path) for issue in issues] == [
        ("invalid_unicode", "$.messages[0].content")
    ]


def test_anthropic_respects_the_part_limit() -> None:
    document = {"messages": [{"content": [{"type": "text", "text": str(i)} for i in range(4)]}]}
    assert codes(document, "anthropic", max_parts=2) == ["too_many_parts"]


def test_anthropic_respects_the_structural_error_limit() -> None:
    document = {"messages": [{"content": [{"type": "text"}, {}, {}, {}]}]}
    assert codes(document, "anthropic", max_parts=2) == ["too_many_issues"]


# --------------------------------------------------------------------------
# Google Gemini
# --------------------------------------------------------------------------


def test_gemini_preserves_part_order(png_base64: str, wav_base64: str) -> None:
    document = {
        "contents": [
            {"role": "user", "parts": [{"text": "Describe"}]},
            {
                "role": "user",
                "parts": [
                    {"inline_data": {"mime_type": "image/png", "data": png_base64}},
                    {"inline_data": {"mime_type": "audio/wav", "data": wav_base64}},
                    {
                        "file_data": {
                            "mime_type": "video/mp4",
                            "file_uri": "https://media.example.org/a.mp4",
                        }
                    },
                ],
            },
        ]
    }
    manifest = normalize(document, policy("gemini", remote=ALLOWED_HOST))
    assert [(part.kind, part.source) for part in manifest.parts] == [
        ("text", "text"),
        ("image", "inline"),
        ("audio", "inline"),
        ("video", "remote"),
    ]
    assert [part.path for part in manifest.parts] == [
        "$.contents[0].parts[0]",
        "$.contents[1].parts[0]",
        "$.contents[1].parts[1]",
        "$.contents[1].parts[2]",
    ]


def test_gemini_inline_data_matches_the_equivalent_default_payload(png_base64: str) -> None:
    adapted = normalize(
        gemini_parts({"inline_data": {"mime_type": "image/png", "data": png_base64}}),
        policy("gemini"),
    )
    native = normalize([{"type": "image", "data": png_base64, "mime_type": "image/png"}])
    assert adapted.parts[0].fingerprint == native.parts[0].fingerprint


def test_gemini_file_data_is_denied_by_default() -> None:
    document = gemini_parts(
        {"file_data": {"mime_type": "image/png", "file_uri": "https://media.example.org/a.png"}}
    )
    issues = validate(document, policy("gemini"))
    assert [(issue.code, issue.path) for issue in issues] == [
        ("remote_url_disabled", "$.contents[0].parts[0].file_data.file_uri")
    ]


def test_gemini_file_data_uses_the_shared_allowlist() -> None:
    document = gemini_parts(
        {"file_data": {"mime_type": "image/png", "file_uri": "https://media.example.org/a.png"}}
    )
    manifest = normalize(document, policy("gemini", remote=ALLOWED_HOST))
    assert manifest.parts[0].locator == "https://media.example.org/a.png"
    assert manifest.parts[0].mime_type == "image/png"


def test_gemini_files_api_uri_is_not_a_fetchable_reference() -> None:
    document = gemini_parts({"file_data": {"mime_type": "image/png", "file_uri": "files/abc123"}})
    assert codes(document, "gemini", remote=ALLOWED_HOST) == ["url_scheme"]


def test_gemini_oversized_inline_data_hits_the_existing_cap(png_base64: str) -> None:
    document = gemini_parts({"inline_data": {"mime_type": "image/png", "data": png_base64}})
    issues = validate(document, tiny_image_policy("gemini"))
    assert [(issue.code, issue.path) for issue in issues] == [
        ("part_too_large", "$.contents[0].parts[0].inline_data.data")
    ]


def test_gemini_signature_check_still_applies(wav_base64: str) -> None:
    document = gemini_parts({"inline_data": {"mime_type": "image/png", "data": wav_base64}})
    assert codes(document, "gemini") == ["signature_mismatch"]


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (["contents"], ["payload_type"]),
        ({"model": "gemini"}, ["payload_shape"]),
        ({"contents": "user"}, ["contents_type"]),
        ({"contents": []}, ["empty_content"]),
        ({"contents": [5]}, ["message_type"]),
        ({"contents": [{"parts": {}}]}, ["content_type"]),
        ({"contents": [{"parts": [9]}]}, ["part_type"]),
        ({"contents": [{"parts": [{"function_call": {}}]}]}, ["unsupported_part"]),
        ({"contents": [{"parts": [{"text": 1}]}]}, ["text_type"]),
        ({"contents": [{"parts": [{"inline_data": "x"}]}]}, ["media_container"]),
        ({"contents": [{"parts": [{"inline_data": {}}]}]}, ["mime_type_field"]),
        ({"contents": [{"parts": [{"inline_data": {"mime_type": "png"}}]}]}, ["invalid_mime"]),
        (
            {"contents": [{"parts": [{"inline_data": {"mime_type": "application/pdf"}}]}]},
            ["unsupported_media_kind"],
        ),
        (
            {"contents": [{"parts": [{"inline_data": {"mime_type": "image/png"}}]}]},
            ["missing_media"],
        ),
        (
            {"contents": [{"parts": [{"file_data": {"mime_type": "image/png"}}]}]},
            ["missing_media"],
        ),
    ],
)
def test_gemini_malformed_envelopes(document: Any, expected: list[str]) -> None:
    assert codes(document, "gemini") == expected


def test_gemini_rejects_a_part_that_combines_union_fields(png_base64: str) -> None:
    document = gemini_parts(
        {"text": "hello", "inline_data": {"mime_type": "image/png", "data": png_base64}}
    )
    issues = validate(document, policy("gemini"))
    assert issues[0].code == "ambiguous_media"
    assert "text, inline_data" in issues[0].message


def test_gemini_text_part_records_its_own_value_path() -> None:
    assert paths({"contents": [{"parts": [{"text": 1}]}]}, "gemini") == [
        "$.contents[0].parts[0].text"
    ]


# --------------------------------------------------------------------------
# Ollama
# --------------------------------------------------------------------------


def test_ollama_generate_orders_the_prompt_before_its_images(png_base64: str) -> None:
    """Images are positional siblings, so the adapter fixes prompt-then-images."""

    document = {
        "model": "llava",
        "prompt": "What is in this picture?",
        "stream": False,
        "images": [png_base64, png_base64],
    }
    manifest = normalize(document, policy("ollama"))
    assert [(part.ordinal, part.kind, part.path) for part in manifest.parts] == [
        (0, "text", "$.prompt"),
        (1, "image", "$.images[0]"),
        (2, "image", "$.images[1]"),
    ]


def test_ollama_image_order_is_independent_of_json_key_order(png_base64: str) -> None:
    document = json.loads(
        json.dumps({"images": [png_base64], "prompt": "What is in this picture?"})
    )
    manifest = normalize(document, policy("ollama"))
    assert [part.kind for part in manifest.parts] == ["text", "image"]


def test_ollama_chat_groups_images_with_their_own_message(png_base64: str) -> None:
    document = {
        "model": "llava",
        "messages": [
            {"role": "user", "content": "first", "images": [png_base64]},
            {"role": "assistant", "content": "second"},
        ],
    }
    manifest = normalize(document, policy("ollama"))
    assert [part.path for part in manifest.parts] == [
        "$.messages[0].content",
        "$.messages[0].images[0]",
        "$.messages[1].content",
    ]


def test_ollama_derives_the_media_type_from_the_signature(png_base64: str) -> None:
    manifest = normalize({"prompt": "p", "images": [png_base64]}, policy("ollama"))
    assert manifest.parts[1].mime_type == "image/png"
    native = normalize([{"type": "image", "data": png_base64, "mime_type": "image/png"}])
    assert manifest.parts[1].fingerprint == native.parts[0].fingerprint


def test_ollama_rejects_an_unrecognizable_image() -> None:
    issues = validate({"prompt": "p", "images": [UNRECOGNIZED_BASE64]}, policy("ollama"))
    assert [(issue.code, issue.path) for issue in issues] == [
        ("undeclared_media_type", "$.images[0]")
    ]
    assert issues[0].hint is not None


def test_ollama_derived_media_type_must_suit_the_image_part_kind(wav_base64: str) -> None:
    """A recognized signature still has to be allowed for the kind it lands in."""

    assert codes({"prompt": "p", "images": [wav_base64]}, "ollama") == ["mime_not_allowed"]


def test_ollama_derived_media_type_still_meets_the_mime_allowlist(png_base64: str) -> None:
    restricted = policy(
        "ollama",
        allowed_mime_types={
            "text": frozenset({"text/plain"}),
            "image": frozenset({"image/jpeg"}),
            "audio": frozenset(),
            "video": frozenset(),
        },
    )
    assert [
        issue.code for issue in validate({"prompt": "p", "images": [png_base64]}, restricted)
    ] == ["mime_not_allowed"]


def test_ollama_oversized_image_hits_the_existing_cap(png_base64: str) -> None:
    issues = validate({"prompt": "p", "images": [png_base64]}, tiny_image_policy("ollama"))
    assert [(issue.code, issue.path) for issue in issues] == [("part_too_large", "$.images[0]")]


def test_ollama_image_is_never_a_remote_reference() -> None:
    """The envelope carries no locator, so a URL is decoded as Base64 and refused."""

    document = {"prompt": "p", "images": ["https://media.example.org/a.png"]}
    assert codes(document, "ollama", remote=ALLOWED_HOST) == ["invalid_base64"]


def test_ollama_accepts_a_data_url_image(png_data_url: str) -> None:
    manifest = normalize({"prompt": "p", "images": [png_data_url]}, policy("ollama"))
    assert manifest.parts[1].mime_type == "image/png"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ([], ["payload_type"]),
        ({"model": "llava"}, ["payload_shape"]),
        ({"prompt": "p", "messages": []}, ["ambiguous_envelope"]),
        ({"prompt": 3}, ["text_type"]),
        ({"messages": "user"}, ["messages_type"]),
        ({"messages": []}, ["empty_content"]),
        ({"messages": [None]}, ["message_type"]),
        ({"messages": [{"role": "user"}]}, ["content_type"]),
        ({"prompt": "p", "images": "x"}, ["content_type"]),
        ({"prompt": "p", "images": [None]}, ["missing_media"]),
        ({"prompt": "p", "images": [""]}, ["missing_media"]),
    ],
)
def test_ollama_malformed_envelopes(document: Any, expected: list[str]) -> None:
    assert codes(document, "ollama") == expected


def test_ollama_image_errors_report_their_own_index() -> None:
    document = {"messages": [{"role": "user", "content": "hi", "images": ["", ""]}]}
    assert paths(document, "ollama") == [
        "$.messages[0].images[0]",
        "$.messages[0].images[1]",
    ]


def test_ollama_prompt_without_images_is_a_plain_text_request() -> None:
    manifest = normalize({"model": "llama3", "prompt": "hello"}, policy("ollama"))
    assert manifest.part_count == 1
    assert manifest.parts[0].text == "hello"


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def test_parse_document_accepts_an_envelope_keyword(png_base64: str) -> None:
    specs = parse_document({"prompt": "p", "images": [png_base64]}, 8, envelope="ollama")
    assert [spec.value_path for spec in specs] == ["$.prompt", "$.images[0]"]


def test_parse_document_still_defaults_to_the_original_contract() -> None:
    with pytest.raises(PayloadValidationError, match=r"payload_shape|expected a content array"):
        parse_document({"contents": []}, 8)


def test_cli_reads_a_named_envelope(png_base64: str) -> None:
    stdout = StringIO()
    document = json.dumps(
        anthropic_image({"type": "base64", "media_type": "image/png", "data": png_base64})
    )
    exit_code = run(
        ["validate", "-", "--envelope", "anthropic"],
        stdin=StringIO(document),
        stdout=stdout,
        stderr=StringIO(),
    )
    assert exit_code == 0
    assert json.loads(stdout.getvalue())["part_count"] == 1


def test_cli_defaults_to_the_original_envelope() -> None:
    stderr = StringIO()
    exit_code = run(
        ["validate", "-", "--json-errors"],
        stdin=StringIO('{"contents":[{"parts":[{"text":"hi"}]}]}'),
        stdout=StringIO(),
        stderr=stderr,
    )
    assert exit_code == 2
    assert json.loads(stderr.getvalue())["errors"][0]["code"] == "payload_shape"


def test_cli_rejects_an_unknown_envelope_name() -> None:
    with pytest.raises(SystemExit) as error:
        run(["validate", "-", "--envelope", "openai"], stdin=StringIO("[]"))
    assert error.value.code == 2
