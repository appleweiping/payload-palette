from __future__ import annotations

import base64
import json
from dataclasses import replace

import pytest

from payload_palette import (
    Manifest,
    NormalizationPolicy,
    NormalizedPart,
    RemoteURLPolicy,
    normalize,
    validate,
)
from payload_palette.errors import PayloadValidationError
from payload_palette.policy import DEFAULT_MIME_TYPES


def test_order_and_text_are_preserved(png_data_url: str) -> None:
    manifest = normalize(
        [
            {"type": "text", "text": "before"},
            {"type": "image_url", "image_url": {"url": png_data_url}},
            {"type": "text", "text": "after"},
        ]
    )
    assert [part.kind for part in manifest.parts] == ["text", "image", "text"]
    assert [part.ordinal for part in manifest.parts] == [0, 1, 2]
    assert manifest.parts[0].text == "before"
    assert manifest.parts[2].text == "after"


def test_manifest_does_not_embed_inline_base64(png_base64: str) -> None:
    manifest = normalize(
        [{"type": "image", "data": png_base64, "mime_type": "image/png"}]
    ).to_dict()
    assert png_base64 not in str(manifest)
    assert manifest["parts"][0]["locator"] == "inline"


def test_manifest_fingerprint_is_deterministic() -> None:
    payload = [{"type": "text", "text": "same"}]
    assert normalize(payload).fingerprint == normalize(payload).fingerprint
    different = normalize([{"type": "text", "text": "other"}])
    assert normalize(payload).fingerprint != different.fingerprint


def test_url_canonical_equivalents_have_the_same_fingerprint() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("cdn.example.org",)))
    urls = (
        "https://cdn.example.org/~user",
        "https://CDN.EXAMPLE.ORG.:443/%7euser",
        "https://cdn.example.org/a/../~user#ignored",
    )
    fingerprints = {
        normalize([{"type": "image_url", "image_url": url}], policy).parts[0].fingerprint
        for url in urls
    }
    assert len(fingerprints) == 1


def test_data_url_mime_conflict(png_data_url: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image", "data": png_data_url, "mime_type": "image/jpeg"}])
    assert error.value.issues[0].code == "mime_conflict"


def test_signature_mismatch(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image", "data": png_base64, "mime_type": "image/jpeg"}])
    assert error.value.issues[0].code == "signature_mismatch"


def test_signature_check_can_be_disabled(png_base64: str) -> None:
    policy = NormalizationPolicy(verify_known_signatures=False)
    part = normalize(
        [{"type": "image", "data": png_base64, "mime_type": "image/jpeg"}], policy
    ).parts[0]
    assert part.mime_type == "image/jpeg"


def test_disallowed_mime(png_base64: str) -> None:
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "video", "data": png_base64, "mime_type": "application/pdf"}])
    assert error.value.issues[0].code == "mime_not_allowed"


def test_part_size_limit(png_base64: str) -> None:
    policy = NormalizationPolicy(
        max_bytes_by_kind={"text": 100, "image": 2, "audio": 100, "video": 100}
    )
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image", "data": png_base64, "mime_type": "image/png"}], policy)
    assert error.value.issues[0].code == "part_too_large"


def test_text_character_limit() -> None:
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "text", "text": "abcd"}], NormalizationPolicy(max_text_characters=3))
    assert error.value.issues[0].code == "text_too_long"


def test_text_mime_policy_is_enforced() -> None:
    allowed = dict(DEFAULT_MIME_TYPES)
    allowed["text"] = frozenset()
    policy = NormalizationPolicy(allowed_mime_types=allowed)
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "text", "text": "blocked"}], policy)
    assert error.value.issues[0].code == "mime_not_allowed"
    assert error.value.issues[0].path == "$[0].text"


def test_message_content_string_errors_use_the_actual_value_path() -> None:
    document = {"messages": [{"content": "too long"}]}
    with pytest.raises(PayloadValidationError) as error:
        normalize(document, NormalizationPolicy(max_text_characters=3))
    assert error.value.issues[0].code == "text_too_long"
    assert error.value.issues[0].path == "$.messages[0].content"


@pytest.mark.parametrize(
    ("part", "code", "path"),
    [
        (
            {"type": "image", "data": "AA==", "mime_type": "image/bmp"},
            "mime_not_allowed",
            "$[0].data",
        ),
        (
            {
                "type": "image",
                "data": "data:image/png;base64,AA==",
                "mime_type": "image/jpeg",
            },
            "mime_conflict",
            "$[0].data",
        ),
        (
            {"type": "image", "data": "data:image/bmp;base64,AA=="},
            "mime_not_allowed",
            "$[0].data",
        ),
    ],
)
def test_mime_policy_and_conflicts_are_rejected_before_base64_decode(
    part: dict[str, str],
    code: str,
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_decode(*args: object, **kwargs: object) -> bytes:
        nonlocal called
        called = True
        raise AssertionError("Base64 decoder must not run before MIME validation")

    monkeypatch.setattr("payload_palette.normalizer.decode_base64", unexpected_decode)
    with pytest.raises(PayloadValidationError) as error:
        normalize([part])
    assert error.value.issues[0].code == code
    assert error.value.issues[0].path == path
    assert called is False


def test_total_inline_limit(png_data_url: str) -> None:
    policy = NormalizationPolicy(max_total_inline_bytes=5)
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image_url", "image_url": png_data_url}], policy)
    assert error.value.issues[0].code == "total_too_large"


def test_total_limit_stops_before_later_invalid_part() -> None:
    payload = [
        {"type": "text", "text": "too long for the total"},
        {"type": "image", "data": "%%%", "mime_type": "image/png"},
    ]
    with pytest.raises(PayloadValidationError) as error:
        normalize(payload, NormalizationPolicy(max_total_inline_bytes=2))
    assert [issue.code for issue in error.value.issues] == ["total_too_large"]


def test_remote_disabled_by_default() -> None:
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image_url", "image_url": "https://cdn.example.org/a.png"}])
    assert error.value.issues[0].code == "remote_url_disabled"


def test_allowlisted_remote_is_not_downloaded() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("cdn.example.org",)))
    part = normalize(
        [{"type": "image_url", "image_url": "https://cdn.example.org/a.png"}], policy
    ).parts[0]
    assert part.source == "remote"
    assert part.byte_length is None
    assert part.locator == "https://cdn.example.org/a.png"


def test_remote_declared_mime_is_checked_and_recorded() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("cdn.example.org",)))
    part = normalize(
        [
            {
                "type": "image_url",
                "image_url": "https://cdn.example.org/a.png",
                "mime_type": "image/png",
            }
        ],
        policy,
    ).parts[0]
    assert part.mime_type == "image/png"


def test_audio_mp4_container_signature_is_compatible() -> None:
    data = base64.b64encode(b"\x00\x00\x00\x18ftypisom").decode()
    part = normalize([{"type": "audio", "data": data, "format": "mp4"}]).parts[0]
    assert part.mime_type == "audio/mp4"


def test_audio_webm_container_signature_is_compatible() -> None:
    data = base64.b64encode(b"\x1aE\xdf\xa3payload").decode()
    part = normalize([{"type": "audio", "data": data, "format": "webm"}]).parts[0]
    assert part.mime_type == "audio/webm"


def test_quicktime_signature_matches_mov_declaration() -> None:
    data = base64.b64encode(b"\x00\x00\x00\x18ftypqt  ").decode()
    part = normalize([{"type": "video", "data": data, "format": "mov"}]).parts[0]
    assert part.mime_type == "video/quicktime"


def test_wildcard_does_not_match_apex() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("*.example.org",)))
    normalize([{"type": "image_url", "image_url": "https://cdn.example.org/a"}], policy)
    with pytest.raises(PayloadValidationError):
        normalize([{"type": "image_url", "image_url": "https://example.org/a"}], policy)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://cdn.example.org/a", "https_required"),
        ("https://user:pass@cdn.example.org/a", "url_credentials"),
        ("https://other.example.org/a", "host_not_allowed"),
    ],
)
def test_remote_security_failures(url: str, code: str) -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("cdn.example.org",)))
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image_url", "image_url": url}], policy)
    assert error.value.issues[0].code == code
    assert error.value.issues[0].path == "$[0].image_url"


def test_private_ip_blocked_even_if_allowlisted() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("127.0.0.1",)))
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "image_url", "image_url": "https://127.0.0.1/a"}], policy)
    assert error.value.issues[0].code == "local_address"


def test_remote_query_is_redacted_but_affects_fingerprint() -> None:
    policy = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("cdn.example.org",)))
    first = normalize(
        [{"type": "image_url", "image_url": "https://cdn.example.org/a?token=one"}], policy
    ).parts[0]
    second = normalize(
        [{"type": "image_url", "image_url": "https://cdn.example.org/a?token=two"}], policy
    ).parts[0]
    assert first.locator == "https://cdn.example.org/a?[redacted]"
    assert first.fingerprint != second.fingerprint


def test_independent_media_errors_are_aggregated() -> None:
    payload = [
        {"type": "image", "data": "%%%", "mime_type": "image/png"},
        {"type": "audio", "data": "%%%", "mime_type": "audio/wav"},
    ]
    with pytest.raises(PayloadValidationError) as error:
        normalize(payload)
    assert len(error.value.issues) == 2
    assert {issue.path for issue in error.value.issues} == {"$[0].data", "$[1].data"}


def test_validate_returns_issues_not_exception() -> None:
    assert validate([{"type": "text", "text": "ok"}]) == ()
    issues = validate([{"type": "unknown"}])
    assert issues[0].code == "unknown_type"


def test_isolated_unicode_surrogate_is_a_structured_input_error() -> None:
    with pytest.raises(PayloadValidationError) as error:
        normalize([{"type": "text", "text": "\ud800"}])
    assert error.value.issues[0].code == "invalid_unicode"


def test_nested_input_audio_is_normalized(wav_base64: str) -> None:
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {"data": wav_base64.rstrip("="), "format": "wav"},
                    }
                ],
            }
        ]
    }
    part = normalize(payload).parts[0]
    assert part.kind == "audio"
    assert part.mime_type == "audio/wav"


def test_policy_mappings_are_immutable() -> None:
    policy = NormalizationPolicy()
    with pytest.raises(TypeError):
        policy.max_bytes_by_kind["image"] = 1  # type: ignore[index]


def test_normalized_attributes_are_deeply_immutable() -> None:
    source = {"name": "before"}
    part = NormalizedPart(
        ordinal=0,
        path="$[0]",
        kind="image",
        source="inline",
        fingerprint="sha256:test",
        attributes=source,
    )
    source["name"] = "caller-mutated"
    assert part.attributes["name"] == "before"
    with pytest.raises(TypeError):
        part.attributes["name"] = "mutated"  # type: ignore[index]


def test_manifest_parts_are_copied_to_an_immutable_tuple() -> None:
    parts = [
        NormalizedPart(
            ordinal=0,
            path="$[0]",
            kind="text",
            source="text",
            fingerprint="sha256:test",
        )
    ]
    manifest = Manifest(parts, "sha256:manifest", 0)  # type: ignore[arg-type]
    parts.clear()
    assert isinstance(manifest.parts, tuple)
    assert manifest.part_count == 1


def test_manifest_rejects_nonstandard_numeric_values() -> None:
    with pytest.raises(ValueError, match="inline_bytes"):
        Manifest((), "sha256:test", float("nan"))  # type: ignore[arg-type]


def test_generated_manifest_is_strict_standard_json() -> None:
    document = normalize([{"type": "text", "text": "hello"}]).to_dict()
    assert json.loads(json.dumps(document, allow_nan=False)) == document


def test_normalize_rejects_wrong_policy_runtime_type() -> None:
    with pytest.raises(ValueError, match="NormalizationPolicy"):
        normalize([{"type": "text", "text": "hello"}], "unsafe")  # type: ignore[arg-type]


def test_replace_policy_keeps_explicit_remote_policy() -> None:
    original = NormalizationPolicy(remote=RemoteURLPolicy(allowed_hosts=("cdn.example.org",)))
    updated = replace(original, max_parts=5)
    assert updated.remote.allowed_hosts == ("cdn.example.org",)
