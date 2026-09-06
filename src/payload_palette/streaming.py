"""Normalize a request that is too large to hold in memory.

:func:`payload_palette.normalize_json_bytes` needs the whole body, its decoded
text, and the parsed document at once.  For a request carrying one large inline
image that is roughly three copies of the media before any manifest exists.

The functions here read the same documents from a byte stream and produce the
same :class:`~payload_palette.models.Manifest`, with peak memory following the
*structure* of the request rather than the size of its media.  Nothing here
performs network or filesystem I/O on the caller's behalf beyond reading the
stream it is given; :func:`normalize_path` is the one convenience that opens a
file, and it is explicit about doing so.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Any

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.ingress import DEFAULT_MAX_INPUT_BYTES, validate_input_limit
from payload_palette.jsonstream import StreamStatistics, parse_json_stream
from payload_palette.models import LARGE_VALUE_PREFIX_CHARACTERS, Manifest
from payload_palette.normalizer import normalize
from payload_palette.policy import MAX_REMOTE_URL_CHARACTERS, NormalizationPolicy


def large_value_threshold(policy: NormalizationPolicy) -> int:
    """Return the length above which a string is measured instead of kept.

    The threshold is strictly greater than every limit that would accept a
    string of that length for anything other than inline media, so a value the
    stream declines to materialize is one the pipeline was going to refuse
    anyway -- as text over ``max_text_characters``, or as a URL over
    ``MAX_REMOTE_URL_CHARACTERS``.  ``normalizer`` depends on exactly this:
    it answers both cases from the recorded length without the characters.
    """

    return (
        max(
            policy.max_text_characters,
            MAX_REMOTE_URL_CHARACTERS,
            LARGE_VALUE_PREFIX_CHARACTERS,
        )
        + 1
    )


def decode_stream(
    stream: IO[bytes],
    policy: NormalizationPolicy | None = None,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> tuple[Any, StreamStatistics]:
    """Decode one JSON request from a byte stream under ``policy``'s limits.

    Returns the document and what the parse actually cost, so a caller can
    assert on the retained size rather than trust a docstring.  Strings past the
    threshold appear as :class:`~payload_palette.models.LargeValue`; pass the
    document to :func:`payload_palette.normalize` to turn it into a manifest.
    """

    active = _policy(policy)
    maximum = validate_input_limit(max_input_bytes)
    return parse_json_stream(
        stream,
        max_input_bytes=maximum,
        large_value_threshold=large_value_threshold(active),
        media_max_bytes=max(active.max_bytes_by_kind.values(), default=0),
        allow_url_safe_base64=active.allow_url_safe_base64,
    )


def normalize_stream(
    stream: IO[bytes],
    policy: NormalizationPolicy | None = None,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> Manifest:
    """Validate a streamed request and return the same manifest as ``normalize``.

    A document accepted here is accepted by the buffered path and yields an
    identical manifest, fingerprint included.  The two can name different issue
    codes only for a document that breaks more than one rule at once, because
    each reports the first fault it is able to see; ``docs/streaming.md`` gives
    the worked example.
    """

    active = _policy(policy)
    document, _statistics = decode_stream(stream, active, max_input_bytes=max_input_bytes)
    return normalize(document, active)


def normalize_path(
    path: str | Path,
    policy: NormalizationPolicy | None = None,
    *,
    max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES,
) -> Manifest:
    """Open one file and normalize it as a stream.

    This is the only function in the package that touches the filesystem, and it
    reads exactly the path it is given: no path resolution, no following of
    references found inside the document.
    """

    source = Path(path)
    try:
        with source.open("rb") as handle:
            return normalize_stream(handle, policy, max_input_bytes=max_input_bytes)
    except OSError as exc:
        raise PayloadValidationError(
            [ValidationIssue("input_read", f"cannot read input: {exc}", str(source))]
        ) from exc


def _policy(policy: NormalizationPolicy | None) -> NormalizationPolicy:
    if policy is not None and not isinstance(policy, NormalizationPolicy):
        raise ValueError("policy must be a NormalizationPolicy or None")
    return NormalizationPolicy() if policy is None else policy


__all__ = [
    "decode_stream",
    "large_value_threshold",
    "normalize_path",
    "normalize_stream",
]
