from __future__ import annotations

import pytest

from payload_palette.errors import PayloadValidationError
from payload_palette.policy import (
    MAX_POLICY_BYTES,
    MAX_POLICY_PARTS,
    MAX_POLICY_TEXT_CHARACTERS,
    MAX_REMOTE_URL_CHARACTERS,
    NormalizationPolicy,
    RemoteURLPolicy,
    _remove_dot_segments,
)


@pytest.mark.parametrize("host", ["", "bad host", "-bad.example", "example..org"])
def test_invalid_allowlist_host(host: str) -> None:
    with pytest.raises(ValueError):
        RemoteURLPolicy(allowed_hosts=(host,))


def test_allowlist_hosts_are_normalized() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("CDN.EXAMPLE.ORG.",))
    assert policy.allowed_hosts == ("cdn.example.org",)


def test_direct_scheme_validation() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("ftp://cdn.example.org/a", "$")
    assert error.value.issues[0].code == "url_scheme"


def test_missing_url_host() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https:///media.png", "$")
    assert error.value.issues[0].code == "url_host"


def test_invalid_port_is_structured() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https://cdn.example.org:invalid/a", "$")
    assert error.value.issues[0].code == "invalid_url"


def test_malformed_ipv6_url_is_structured() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https://[::1/a", "$")
    assert error.value.issues[0].code == "invalid_url"


def test_localhost_name_is_blocked() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("localhost",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https://localhost/a", "$")
    assert error.value.issues[0].code == "local_address"


def test_explicitly_permitted_ipv6_is_rendered_with_brackets() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("::1",), block_local_addresses=False)
    display, canonical = policy.validate("https://[::1]:8443/a#ignored", "$")
    assert display == "https://[::1]:8443/a"
    assert canonical == display


def test_http_can_be_enabled_explicitly() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",), require_https=False)
    display, _ = policy.validate("http://cdn.example.org", "$")
    assert display == "http://cdn.example.org/"


def test_unicode_url_host_matches_idna_allowlist() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("xn--bcher-kva.example",))
    display, _ = policy.validate("https://bücher.example/a", "$")
    assert display == "https://xn--bcher-kva.example/a"


def test_unicode_allowlist_host_is_normalized_with_idna() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("bücher.example",))
    assert policy.allowed_hosts == ("xn--bcher-kva.example",)


@pytest.mark.parametrize(
    "host",
    ["127.1", "2130706433", "0177.0.0.1", "0x7f000001"],
)
def test_ambiguous_legacy_ipv4_allowlist_hosts_are_rejected(host: str) -> None:
    with pytest.raises(ValueError, match="legacy IPv4"):
        RemoteURLPolicy(allowed_hosts=(host,))


@pytest.mark.parametrize(
    "host",
    [
        "\uff11\uff12\uff17.\uff11",
        "\uff11\uff12\uff17\uff0e\uff11",
        "\uff12\uff11\uff13\uff10\uff17\uff10\uff16\uff14\uff13\uff13",
        "\uff10x\uff17f\uff10\uff10\uff10\uff10\uff10\uff10\uff11",
        "\uff10\uff11\uff17\uff17.\uff10.\uff10.\uff11",
        "\uff11\uff12\uff17\uff0e\uff10\uff0e\uff10\uff0e\uff11",
    ],
)
def test_idna_mapped_ip_literals_are_rejected_after_normalization(host: str) -> None:
    with pytest.raises(ValueError, match="IP"):
        RemoteURLPolicy(allowed_hosts=(host,))


@pytest.mark.parametrize("host", ["*.1", "*.\uff11\uff12\uff17\uff0e\uff11", "*.127.0.0.1"])
def test_wildcards_cannot_target_numeric_ip_suffixes(host: str) -> None:
    with pytest.raises(ValueError):
        RemoteURLPolicy(allowed_hosts=(host,))


@pytest.mark.parametrize(
    "url",
    [
        "https://evil\x00.example.org/a",
        "https://cdn.exam\nple.org/a",
        "https://bad_host.example.org/a",
        "https://cdn.example.org/a\\b",
        "https://cdn.example.org/a path",
        "https://cdn.example.org/%ZZ",
    ],
)
def test_url_parser_differential_inputs_are_rejected(url: str) -> None:
    policy = RemoteURLPolicy(allowed_hosts=("*.example.org", "cdn.example.org"))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate(url, "$")
    assert error.value.issues[0].code == "invalid_url"


@pytest.mark.parametrize("codepoint", [0x80, 0x81, 0x9F])
def test_c1_url_controls_are_rejected(codepoint: int) -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate(f"https://cdn.example.org/a{chr(codepoint)}b", "$")
    assert error.value.issues[0].code == "invalid_url"


def test_ipv6_zone_identifier_is_rejected() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("2001:4860::1",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https://[2001:4860::1%25eth0]/a", "$")
    assert error.value.issues[0].code == "invalid_url"


def test_multiple_host_trailing_dots_are_rejected() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https://cdn.example.org../a", "$")
    assert error.value.issues[0].code == "invalid_url"


def test_remote_url_length_is_rejected_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))

    def unexpected_urlsplit(value: str) -> None:
        raise AssertionError(f"urlsplit unexpectedly received {len(value)} characters")

    monkeypatch.setattr("payload_palette.policy.urlsplit", unexpected_urlsplit)
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("h" * (MAX_REMOTE_URL_CHARACTERS + 1), "$")
    assert error.value.issues[0].code == "url_too_long"


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "10.0.0.1",
        "100.64.0.1",
        "127.0.0.1",
        "169.254.1.1",
        "172.16.0.1",
        "192.0.0.8",
        "192.0.2.1",
        "192.88.99.1",
        "192.88.99.2",
        "192.168.0.1",
        "198.18.0.1",
        "198.51.100.1",
        "203.0.113.1",
        "224.0.0.1",
        "240.0.0.1",
        "::",
        "::1",
        "::ffff:127.0.0.1",
        "64:ff9b::127.0.0.1",
        "64:ff9b:1::1",
        "100::1",
        "2001::1",
        "2001:2::1",
        "2001:db8::1",
        "2002::1",
        "3fff::1",
        "4000::1",
        "5f00::1",
        "fc00::1",
        "fe80::1",
        "fec0::1",
        "ff02::1",
    ],
)
def test_non_global_literal_table_is_blocked_consistently(host: str) -> None:
    policy = RemoteURLPolicy(allowed_hosts=(host,))
    rendered = f"[{host}]" if ":" in host else host
    with pytest.raises(PayloadValidationError) as error:
        policy.validate(f"https://{rendered}/a", "$")
    assert error.value.issues[0].code == "local_address"


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "192.0.0.9",
        "192.0.0.10",
        "2001:1::1",
        "2001:1::2",
        "2001:1::3",
        "2001:3::1",
        "2001:4:112::1",
        "2001:20::1",
        "2001:30::1",
        "2001:4860:4860::8888",
        "::ffff:8.8.8.8",
        "64:ff9b::8.8.8.8",
    ],
)
def test_global_literal_table_is_allowed_consistently(host: str) -> None:
    policy = RemoteURLPolicy(allowed_hosts=(host,))
    rendered = f"[{host}]" if ":" in host else host
    assert policy.validate(f"https://{rendered}/a", "$")[0].endswith("/a")


def test_url_canonicalization_normalizes_safe_equivalences() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",))
    display, canonical = policy.validate(
        "HTTPS://CDN.EXAMPLE.ORG.:443/a/../%7euser?q=%7e#fragment",
        "$",
    )
    assert display == "https://cdn.example.org/~user?[redacted]"
    assert canonical == "https://cdn.example.org/~user?q=~"


def test_http_default_port_is_removed_when_http_is_enabled() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("cdn.example.org",), require_https=False)
    assert policy.validate("http://cdn.example.org:80/a", "$")[1] == ("http://cdn.example.org/a")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"allowed_hosts": "cdn.example.org"},
        {"require_https": 1},
        {"block_local_addresses": "false"},
        {"redact_query": None},
    ],
)
def test_remote_policy_rejects_wrong_runtime_types(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        RemoteURLPolicy(**kwargs)  # type: ignore[arg-type]


def test_remote_policy_rejects_non_string_host_entry() -> None:
    with pytest.raises(ValueError, match="strings"):
        RemoteURLPolicy(allowed_hosts=(1,))  # type: ignore[arg-type]


def test_url_host_cannot_contain_an_allowlist_wildcard() -> None:
    policy = RemoteURLPolicy(allowed_hosts=("*.example.org",))
    with pytest.raises(PayloadValidationError) as error:
        policy.validate("https://*.example.org/media.png", "$")
    assert error.value.issues[0].code == "invalid_url"


def test_invalid_unicode_in_wildcard_allowlist_is_rejected() -> None:
    with pytest.raises(ValueError, match="invalid host pattern"):
        RemoteURLPolicy(allowed_hosts=("*.\u200d.example",))


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("../a", "a"),
        ("./a", "a"),
        ("/./a", "/a"),
        ("/.", "/"),
        ("/a/b/../c", "/a/c"),
        ("/a/b/..", "/a/"),
        (".", ""),
        ("..", ""),
    ],
)
def test_rfc_dot_segment_removal_boundaries(path: str, expected: str) -> None:
    assert _remove_dot_segments(path) == expected


def test_dot_segment_removal_handles_many_segments_without_recursion() -> None:
    path = "/a" * 50_000 + "/../tail"
    result = _remove_dot_segments(path)
    assert result.endswith("/tail")
    assert result.count("/a") == 49_999


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_parts": 0},
        {"max_text_characters": -1},
        {"max_total_inline_bytes": -1},
        {"max_bytes_by_kind": {"text": 1}},
        {
            "max_bytes_by_kind": {
                "text": 1,
                "image": -1,
                "audio": 1,
                "video": 1,
            }
        },
    ],
)
def test_invalid_normalization_policy(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        NormalizationPolicy(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_parts": True},
        {"max_parts": 1.5},
        {"max_parts": float("nan")},
        {"max_parts": MAX_POLICY_PARTS + 1},
        {"max_text_characters": float("inf")},
        {"max_text_characters": MAX_POLICY_TEXT_CHARACTERS + 1},
        {"max_total_inline_bytes": float("nan")},
        {"max_total_inline_bytes": MAX_POLICY_BYTES + 1},
        {
            "max_bytes_by_kind": {
                "text": 1,
                "image": float("nan"),
                "audio": 1,
                "video": 1,
            }
        },
        {
            "max_bytes_by_kind": {
                "text": 1,
                "image": 1,
                "audio": 1,
                "video": 1,
                "typo": 1,
            }
        },
        {"allowed_mime_types": {"text": "text/plain"}},
        {"remote": object()},
        {"verify_known_signatures": 1},
    ],
)
def test_policy_rejects_non_integer_nonfinite_and_oversized_limits(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        NormalizationPolicy(**kwargs)  # type: ignore[arg-type]


def _complete_mime_policy(text_values: object) -> dict[str, object]:
    return {
        "text": text_values,
        "image": {"image/png"},
        "audio": set(),
        "video": set(),
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes_by_kind": []},
        {"allowed_mime_types": []},
        {"allowed_mime_types": _complete_mime_policy("text/plain")},
        {"allowed_mime_types": _complete_mime_policy(1)},
        {"allowed_mime_types": _complete_mime_policy({1})},
        {"allowed_mime_types": _complete_mime_policy({"not a mime"})},
    ],
)
def test_policy_rejects_invalid_mapping_and_mime_collection_shapes(
    kwargs: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        NormalizationPolicy(**kwargs)  # type: ignore[arg-type]


def test_allowed_mime_types_must_be_complete_and_normalized() -> None:
    defaults = {
        "text": {"text/plain"},
        "image": {"IMAGE/PNG"},
        "audio": set(),
        "video": set(),
    }
    with pytest.raises(ValueError, match="already be normalized"):
        NormalizationPolicy(allowed_mime_types=defaults)  # type: ignore[arg-type]


def test_policy_size_check_reports_decoded_size() -> None:
    policy = NormalizationPolicy(max_bytes_by_kind={"text": 1, "image": 1, "audio": 1, "video": 1})
    with pytest.raises(PayloadValidationError) as error:
        policy.check_size("image", 2, "$.image")
    assert "2 bytes" in error.value.issues[0].message


def test_url_safe_base64_is_disabled_by_default_and_requires_a_boolean() -> None:
    assert NormalizationPolicy().allow_url_safe_base64 is False
    with pytest.raises(ValueError, match="allow_url_safe_base64 must be a boolean"):
        NormalizationPolicy(allow_url_safe_base64=1)  # type: ignore[arg-type]
