"""Closed, self-authored mutation rules; no dynamic recipe evaluation or network."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from dataclasses import replace
from typing import Any

from benchmarks.corpus import CorpusCase, canonical, digest
from payload_palette.models import ENVELOPE_NAMES


def _text(envelope: str, text: object) -> dict[str, Any]:
    if envelope == "gemini":
        return {"contents": [{"role": "user", "parts": [{"text": text}]}]}
    if envelope == "ollama":
        return {"messages": [{"role": "user", "content": text}]}
    return {"messages": [{"role": "user", "content": [{"type": "text", "text": text}]}]}


def _image(envelope: str, encoded: str, mime: str = "image/png") -> dict[str, Any]:
    if envelope == "anthropic":
        part = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}}
    elif envelope == "gemini":
        return {"contents": [{"parts": [{"inline_data": {"mime_type": mime, "data": encoded}}]}]}
    elif envelope == "ollama":
        return {"messages": [{"role": "user", "content": "", "images": [encoded]}]}
    else:
        part = {"type": "image", "mime_type": mime, "data": encoded}
    return {"messages": [{"role": "user", "content": [part]}]}


def _url(envelope: str, url: str) -> dict[str, Any]:
    if envelope == "gemini":
        return {
            "contents": [{"parts": [{"file_data": {"mime_type": "image/png", "file_uri": url}}]}]
        }
    if envelope == "anthropic":
        part = {"type": "image", "source": {"type": "url", "url": url}}
    else:
        part = {"type": "image", "url": url, "mime_type": "image/png"}
    return {"messages": [{"role": "user", "content": [part]}]}


def _metadata(document: bytes, value: bytes, name: bytes = b"probe") -> bytes:
    return document[:-1] + b',"' + name + b'":' + value + b"}"


def authored_cases(seed: int, repetition: int) -> Iterator[CorpusCase]:
    entropy = bytes.fromhex(digest(canonical([seed, repetition])))
    text = "original-control-" + entropy[:6].hex()
    # Recognizable synthetic prefixes, not complete decodable media containers.
    png = b"\x89PNG\r\n\x1a\n\xfb\xff" + entropy[:9]
    encoded = base64.b64encode(png).decode("ascii")
    for envelope in ENVELOPE_NAMES:
        original = canonical(_text(envelope, text))
        base = CorpusCase(
            envelope,
            "control",
            "valid",
            "identity",
            (("seed", seed), ("repetition", repetition)),
            "original-text-" + envelope,
            digest(original),
            "payload-palette-original",
            "authored-seeds-v1",
            original,
        )
        yield base

        def case(
            family: str,
            name: str,
            raw: bytes,
            comparison: str = "single",
            profile: tuple[tuple[str, int], ...] = (),
            precedence: tuple[tuple[str, str], ...] = (),
            *,
            original_case: CorpusCase = base,
        ) -> CorpusCase:
            return replace(
                original_case,
                family=family,
                mutation=name,
                comparison=comparison,
                raw=raw,
                profile=profile,
                precedence=precedence,
            )

        for family in ("json", "utf8", "duplicates", "envelope"):
            yield case(family, "original_control", original, "valid")

        for name, json_value in (
            ("missing_value", b""),
            ("trailing_comma", b"[1,]"),
            ("missing_colon", b'{"a" 1}'),
            ("missing_comma", b"[1 2]"),
        ):
            yield case("json", name, _metadata(original, json_value))
        yield case("utf8", "invalid_byte", _metadata(original, b'"\xff"'))
        yield case("duplicates", "duplicate_root_key", _metadata(_metadata(original, b"0"), b"1"))
        for name, token in (
            ("nan", b"NaN"),
            ("infinity", b"Infinity"),
            ("overflow", b"1e999"),
            ("integer_digits", b"1" * 257),
        ):
            yield case("numbers", name, _metadata(original, token))
        yield case("numbers", "finite_control", _metadata(original, b"1.25e-3"), "valid")
        yield case(
            "nesting", "below_depth", _metadata(original, b"[" * 8 + b"0" + b"]" * 8), "valid"
        )
        yield case("nesting", "above_depth", _metadata(original, b"[" * 129 + b"0" + b"]" * 129))
        yield case("unicode", "surrogate", _metadata(original, b'"\\ud800"'))
        yield case("unicode", "paired_control", _metadata(original, b'"\\ud83d\\ude00"'), "valid")
        yield case("unicode", "utf8_control", canonical(_text(envelope, "é中文😀")), "valid")
        yield case("envelope", "missing_required", b"{}")
        yield case("envelope", "wrong_root_type", b"42")
        yield case("envelope", "wrong_text_type", canonical(_text(envelope, 7)))
        wrong = {"contents" if envelope == "gemini" else "messages": "not-an-array"}
        yield case("envelope", "wrong_message_container", canonical(wrong))
        media = canonical(_image(envelope, encoded))
        yield case("base64", "standard_control", media, "valid")
        yield case(
            "base64", "unpadded_control", canonical(_image(envelope, encoded.rstrip("="))), "valid"
        )
        yield case(
            "base64",
            "whitespace_control",
            canonical(_image(envelope, " \n" + encoded + "\t")),
            "valid",
        )
        yield case(
            "base64",
            "urlsafe_control",
            canonical(_image(envelope, base64.urlsafe_b64encode(png).decode())),
            "valid",
            (("urlsafe", 1),),
        )
        for name, value in (
            ("invalid_character", "!" + encoded[1:]),
            ("interior_padding", "A=" + encoded),
            ("truncated_quantum", "A"),
            ("extra_padding", encoded + "===="),
            ("mixed_alphabet", "+_AA"),
            ("urlsafe_disabled", "-_AA"),
        ):
            yield case("base64", name, canonical(_image(envelope, value)))
        yield case(
            "signature",
            "wrong_known_kind",
            canonical(_image(envelope, base64.b64encode(b"RIFF1234WAVEfmt ").decode())),
        )
        yield case("signature", "prefix_control", media, "valid")
        long_encoded = base64.b64encode(png + b"x" * 18000).decode("ascii")
        yield case("base64", "measured_control", canonical(_image(envelope, long_encoded)), "valid")
        yield case("base64", "measured_bad_tail", canonical(_image(envelope, long_encoded + "!")))
        if envelope != "ollama":
            for name, mime in (
                ("unsupported", "image/tiff"),
                ("invalid", "image"),
                ("wrong_kind", "audio/wav"),
            ):
                yield case("mime", name, canonical(_image(envelope, encoded, mime)))
            yield case("mime", "declaration_control", media, "valid")
        if envelope == "default":
            yield case(
                "data_url",
                "header_control",
                canonical(_image(envelope, "data:image/png;base64," + encoded)),
                "valid",
            )
            for name, value in (
                ("missing_comma", "data:image/png;base64"),
                ("not_base64", "data:image/png,abcd"),
                ("mime_conflict", "data:audio/wav;base64," + encoded),
                ("header_too_long", "data:image/png;" + "a" * 1025 + ";base64," + encoded),
            ):
                yield case("data_url", name, canonical(_image(envelope, value)))
            alias = _image(envelope, encoded)
            alias["messages"][0]["content"][0]["url"] = "https://media.example.org/a.png"
            yield case("aliases", "competing_media", canonical(alias))
            yield case("aliases", "one_media_control", media, "valid")
        elif envelope == "gemini":
            alias = _image(envelope, encoded)
            alias["contents"][0]["parts"][0]["text"] = "competing union member"
            yield case("aliases", "competing_union_fields", canonical(alias))
            yield case("aliases", "one_media_control", media, "valid")
            # Published envelope support is snake_case, not an auto-detected SDK shape.
            camel = {
                "contents": [
                    {"parts": [{"inlineData": {"mimeType": "image/png", "data": encoded}}]}
                ]
            }
            yield case("envelope", "unsupported_camel_case", canonical(camel))
        if envelope != "ollama":
            for name, url in (
                ("valid_control", "https://media.example.org/a.png?synthetic=1"),
                ("scheme", "ftp://media.example.org/a.png"),
                ("authority", "https:///a.png"),
                ("credentials", "https://fake-user:fake-pass@media.example.org/a.png"),
                ("host", "https://not-allowed.example.org/a.png"),
                ("port", "https://media.example.org:99999/a.png"),
                ("private", "https://127.0.0.1/a.png"),
                ("escaping", "https://media.example.org/%zz"),
                ("length", "https://media.example.org/" + "a" * 16384),
            ):
                yield case(
                    "url",
                    name,
                    canonical(_url(envelope, url)),
                    "valid" if name == "valid_control" else "single",
                )
        for name, cap in (("below", len(text) + 1), ("exact", len(text)), ("above", len(text) - 1)):
            yield case(
                "limits",
                "text_" + name,
                original,
                "single" if name == "above" else "valid",
                (("text", cap),),
            )
        for name, cap in (("below", len(png) + 1), ("exact", len(png)), ("above", len(png) - 1)):
            yield case(
                "limits",
                "part_" + name,
                media,
                "single" if name == "above" else "valid",
                (("part_bytes", cap),),
            )
            yield case(
                "limits",
                "total_" + name,
                media,
                "single" if name == "above" else "valid",
                (("total_bytes", cap),),
            )
        for name, cap in (
            ("below", len(original) + 1),
            ("exact", len(original)),
            ("above", len(original) - 1),
        ):
            yield case(
                "limits",
                "input_" + name,
                original,
                "single" if name == "above" else "valid",
                (("input_bytes", cap),),
            )
        for name, count in (("below", 1), ("exact", 2), ("above", 3)):
            many = _text(envelope, text)
            field = "contents" if envelope == "gemini" else "messages"
            many[field] = many[field] * count
            yield case(
                "limits",
                "parts_" + name,
                canonical(many),
                "single" if name == "above" else "valid",
                (("parts", 2),),
            )
        yield case(
            "multi_fault",
            "early_syntax_late_utf8",
            b'{"a":@,"b":"\xff"}',
            "multiple",
            precedence=(("input_encoding", "invalid_json"),),
        )


def foreign_cases(seeds: tuple[dict[str, Any], ...]) -> Iterator[CorpusCase]:
    for seed in seeds:
        raw = base64.b64decode(seed["base64"], validate=True)
        label = seed["path"].split("/")[1][0]
        for envelope in ENVELOPE_NAMES:
            document = canonical(_text(envelope, "original-external-token-control"))
            yield CorpusCase(
                envelope,
                "external_json",
                "valid" if label == "y" else "invalid" if label == "n" else "equivalent",
                "wrap_metadata_token",
                (("upstream_class", label),),
                seed["path"],
                seed["sha256"],
                "nst/JSONTestSuite",
                "1ef36fa01286573e846ac449e8683f8833c5b26a",
                _metadata(document, raw),
            )
