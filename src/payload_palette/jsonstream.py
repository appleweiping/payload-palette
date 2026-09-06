"""Incremental, strict JSON decoding for payloads too large to hold in memory.

:func:`payload_palette.ingress.decode_json_bytes` reads one already-buffered
body.  It is the right tool when the body is small, but it holds the raw bytes,
their decoded text, and the parsed document at the same time, and a request
carrying one large inline image pays for that media three times over.

This module reads the same grammar from a byte stream in bounded chunks and
applies every rule the buffered decoder applies: the nesting ceiling, duplicate
object keys, non-standard numeric constants, non-finite floats, oversized
integers, and isolated surrogate code points.  The nesting ceiling is enforced
while parsing rather than by a separate character pass over the whole document,
so it costs nothing extra.

The one thing it does *not* do is materialize a very long string.  A string
longer than the configured threshold is replaced by a :class:`LargeValue`, which
records the string's length and leading characters and -- crucially -- the
Base64 summary computed *as the characters stream past*.  A manifest entry for
inline media needs only a decoded length, a digest, and the leading signature
bytes, so a payload of any size collapses into a few dozen bytes and is never
retained.  See ``docs/streaming.md`` for the resulting contract and its one
deliberate difference from the buffered path.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any

from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.media import (
    MAX_DATA_URL_HEADER_CHARACTERS,
    Base64Digester,
    Base64Summary,
)
from payload_palette.models import LARGE_VALUE_PREFIX_CHARACTERS, DeferredIssue, LargeValue

# One read from the underlying stream.  Large enough that the per-chunk Python
# overhead disappears against the copy itself, small enough that the peak stays
# far below any part limit.
CHUNK_BYTES = 256 * 1024

# Mirrors the buffered decoder's limits so a document is accepted or refused
# identically by either path.
MAX_JSON_DEPTH = 128
MAX_JSON_INTEGER_DIGITS = 256

_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z")
_NUMBER_CHARACTERS = frozenset("-+.eE0123456789")
_WHITESPACE = frozenset(" \t\n\r")
_STRING_STOP = re.compile(r'["\\]|[\x00-\x1f]')
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}


@dataclass(slots=True)
class StreamStatistics:
    """What the parse cost, for callers that log or assert on resource use."""

    byte_count: int = 0
    large_values: int = 0
    largest_value_characters: int = 0
    streamed_media_bytes: int = 0
    peak_retained_characters: int = 0


@dataclass(slots=True)
class _LargeStringState:
    """Accumulates one over-long string into measurements."""

    digester: Base64Digester | None
    prefix: str = ""
    character_count: int = 0
    started: bool = False
    header: str | None = None
    issue: DeferredIssue | None = None


def _issue(
    code: str, message: str, path: str = "$", hint: str | None = None
) -> PayloadValidationError:
    return PayloadValidationError([ValidationIssue(code, message, path, hint)])


class _Source:
    """A chunked UTF-8 character reader with a byte budget.

    Only one chunk of text is live at a time.  Line and column are tracked
    across discarded chunks so a refusal can still name where it happened.
    """

    __slots__ = (
        "_column_base",
        "_decoder",
        "_line_base",
        "_maximum",
        "_stream",
        "buffer",
        "byte_count",
        "exhausted",
        "index",
    )

    def __init__(self, stream: IO[bytes], maximum: int) -> None:
        self._stream = stream
        self._maximum = maximum
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self.buffer = ""
        self.index = 0
        self.byte_count = 0
        self.exhausted = False
        self._line_base = 1
        self._column_base = 0

    def fill(self) -> bool:
        """Make at least one character available; ``False`` at end of input.

        Callers arrange for the buffer to be fully consumed first -- ``_extend``
        hands over exactly the consumed prefix -- because the line and column
        bookkeeping below charges the whole buffer as read.
        """

        consumed = self.buffer
        if consumed:
            newlines = consumed.count("\n")
            if newlines:
                self._line_base += newlines
                self._column_base = len(consumed) - consumed.rfind("\n") - 1
            else:
                self._column_base += len(consumed)
        self.buffer = ""
        self.index = 0
        while not self.exhausted:
            try:
                raw = self._stream.read(CHUNK_BYTES)
            except OSError as exc:
                raise _issue("input_read", f"cannot read input: {exc}") from exc
            if not isinstance(raw, (bytes, bytearray, memoryview)):
                if raw:
                    raise _issue("input_type", "the input stream must yield bytes")
                raw = b""
            if not raw:
                self.exhausted = True
                try:
                    # A truncated multi-byte sequence surfaces here and nowhere
                    # else; a UTF-8 decoder emits characters as soon as they are
                    # complete, so this flush never yields text of its own.
                    self._decoder.decode(b"", final=True)
                except UnicodeError as exc:
                    raise _issue("input_encoding", f"input is not valid UTF-8: {exc}") from exc
                return False
            self.byte_count += len(raw)
            if self.byte_count > self._maximum:
                raise _issue(
                    "input_too_large", f"JSON input exceeds the {self._maximum}-byte limit"
                )
            try:
                text = self._decoder.decode(bytes(raw))
            except UnicodeError as exc:
                raise _issue("input_encoding", f"input is not valid UTF-8: {exc}") from exc
            if text:
                self.buffer = text
                return True
        return False

    def location(self) -> str:
        """Describe the current position for an error message."""

        seen = self.buffer[: self.index]
        newlines = seen.count("\n")
        if newlines:
            line = self._line_base + newlines
            column = len(seen) - seen.rfind("\n")
        else:
            line = self._line_base
            column = self._column_base + len(seen) + 1
        return f" at line {line}, column {column}"


class _Parser:
    """A strict recursive-descent JSON parser over a character stream.

    Recursion is safe here because the nesting ceiling is checked *before*
    descending, so the interpreter stack is bounded by
    :data:`MAX_JSON_DEPTH` regardless of the input.
    """

    __slots__ = ("_allow_url_safe", "_large_threshold", "_media_max_bytes", "_source", "stats")

    def __init__(
        self,
        source: _Source,
        *,
        large_threshold: int,
        media_max_bytes: int,
        allow_url_safe: bool,
    ) -> None:
        self._source = source
        self._large_threshold = large_threshold
        self._media_max_bytes = media_max_bytes
        self._allow_url_safe = allow_url_safe
        self.stats = StreamStatistics()

    # -- character helpers -------------------------------------------------

    def _fail(self, message: str) -> PayloadValidationError:
        return _issue("invalid_json", f"invalid JSON{self._source.location()}: {message}")

    def _peek(self) -> str | None:
        source = self._source
        if source.index < len(source.buffer) or source.fill():
            return source.buffer[source.index]
        return None

    def _take(self) -> str:
        source = self._source
        if source.index >= len(source.buffer) and not source.fill():
            raise self._fail("unexpected end of input")
        character = source.buffer[source.index]
        source.index += 1
        return character

    def _skip_whitespace(self) -> None:
        source = self._source
        while True:
            buffer = source.buffer
            index = source.index
            length = len(buffer)
            while index < length and buffer[index] in _WHITESPACE:
                index += 1
            source.index = index
            if index < length:
                return
            if not source.fill():
                return

    def _expect(self, character: str) -> None:
        found = self._peek()
        if found != character:
            seen = "end of input" if found is None else repr(found)
            raise self._fail(f"expected {character!r} but found {seen}")
        self._source.index += 1

    # -- grammar -----------------------------------------------------------

    def parse(self) -> Any:
        """Read exactly one JSON document and refuse trailing content."""

        self._skip_whitespace()
        if self._peek() is None:
            raise self._fail("input is empty")
        document = self._parse_value(0)
        self._skip_whitespace()
        if self._peek() is not None:
            raise self._fail("trailing data after the top-level value")
        self.stats.byte_count = self._source.byte_count
        return document

    def _parse_value(self, depth: int) -> Any:
        character = self._peek()
        if character is None:
            raise self._fail("unexpected end of input")
        if character == "{":
            return self._parse_object(depth)
        if character == "[":
            return self._parse_array(depth)
        if character == '"':
            return self._parse_string(allow_large=True)
        if character in _NUMBER_CHARACTERS:
            return self._parse_number()
        return self._parse_keyword()

    def _descend(self, depth: int) -> int:
        if depth + 1 > MAX_JSON_DEPTH:
            raise _issue("json_too_deep", f"JSON nesting exceeds the {MAX_JSON_DEPTH}-level limit")
        return depth + 1

    def _parse_object(self, depth: int) -> dict[str, Any]:
        inner = self._descend(depth)
        self._expect("{")
        result: dict[str, Any] = {}
        self._skip_whitespace()
        if self._peek() == "}":
            self._source.index += 1
            return result
        while True:
            self._skip_whitespace()
            if self._peek() != '"':
                raise self._fail("expected a double-quoted object key")
            key = self._parse_string(allow_large=False)
            if not isinstance(key, str):  # pragma: no cover - allow_large=False
                raise self._fail("object keys must be strings")
            if key in result:
                raise _issue("duplicate_json_key", f"JSON object contains duplicate key {key!r}")
            self._skip_whitespace()
            self._expect(":")
            self._skip_whitespace()
            result[key] = self._parse_value(inner)
            self._skip_whitespace()
            character = self._peek()
            if character == ",":
                self._source.index += 1
                continue
            if character == "}":
                self._source.index += 1
                return result
            seen = "end of input" if character is None else repr(character)
            raise self._fail(f"expected ',' or '}}' but found {seen}")

    def _parse_array(self, depth: int) -> list[Any]:
        inner = self._descend(depth)
        self._expect("[")
        result: list[Any] = []
        self._skip_whitespace()
        if self._peek() == "]":
            self._source.index += 1
            return result
        while True:
            self._skip_whitespace()
            result.append(self._parse_value(inner))
            self._skip_whitespace()
            character = self._peek()
            if character == ",":
                self._source.index += 1
                continue
            if character == "]":
                self._source.index += 1
                return result
            seen = "end of input" if character is None else repr(character)
            raise self._fail(f"expected ',' or ']' but found {seen}")

    def _parse_keyword(self) -> Any:
        for literal, value in (("true", True), ("false", False), ("null", None)):
            if self._match(literal):
                return value
        for constant in ("NaN", "Infinity", "-Infinity"):
            if self._match(constant):
                raise _issue(
                    "nonstandard_json_number",
                    f"JSON contains non-standard numeric constant {constant!r}",
                )
        raise self._fail(f"unexpected character {self._peek()!r}")

    def _match(self, literal: str) -> bool:
        """Consume ``literal`` if it is next, without consuming a partial match."""

        source = self._source
        while len(source.buffer) - source.index < len(literal):
            if not self._extend():
                break
        if source.buffer.startswith(literal, source.index):
            source.index += len(literal)
            return True
        return False

    def _extend(self) -> bool:
        """Pull one more chunk while keeping the unconsumed text intact."""

        source = self._source
        # Hand ``fill`` exactly the text that has been consumed, so its line and
        # column bookkeeping still sees it; discarding the whole buffer here
        # would silently lose every newline before the current position.
        remainder = source.buffer[source.index :]
        source.buffer = source.buffer[: source.index]
        if not source.fill():
            source.buffer = remainder
            source.index = 0
            return False
        source.buffer = remainder + source.buffer
        source.index = 0
        return True

    def _parse_number(self) -> int | float:
        source = self._source
        literal: list[str] = []
        while True:
            if source.index >= len(source.buffer) and not source.fill():
                break
            buffer = source.buffer
            index = source.index
            length = len(buffer)
            start = index
            while index < length and buffer[index] in _NUMBER_CHARACTERS:
                index += 1
            literal.append(buffer[start:index])
            source.index = index
            if index < length:
                break
        text = "".join(literal)
        if text == "-" and self._match("Infinity"):
            raise _issue(
                "nonstandard_json_number",
                "JSON contains non-standard numeric constant '-Infinity'",
            )
        if not _NUMBER.match(text):
            raise self._fail(f"malformed number {text!r}")
        if "." in text or "e" in text or "E" in text:
            parsed = float(text)
            if parsed != parsed or parsed in (float("inf"), float("-inf")):
                raise _issue("json_number_range", "JSON number is outside the finite range")
            return parsed
        if len(text.lstrip("-")) > MAX_JSON_INTEGER_DIGITS:
            raise _issue(
                "json_number_range", f"JSON integer exceeds {MAX_JSON_INTEGER_DIGITS} digits"
            )
        return int(text)

    def _string_segments(self) -> Iterator[str]:
        """Yield the decoded contents of one string, in bulk where possible.

        Ordinary runs of characters move with a single slice; only escapes and
        the terminating quote are handled one character at a time.
        """

        source = self._source
        while True:
            if source.index >= len(source.buffer) and not source.fill():
                raise self._fail("unterminated string")
            buffer = source.buffer
            match = _STRING_STOP.search(buffer, source.index)
            if match is None:
                yield buffer[source.index :]
                source.index = len(buffer)
                continue
            stop = match.start()
            if stop > source.index:
                yield buffer[source.index : stop]
            source.index = stop
            character = buffer[stop]
            if character == '"':
                source.index += 1
                return
            if character != "\\":
                raise self._fail(f"unescaped control character {character!r} in a string")
            source.index += 1
            yield self._parse_escape()

    def _hex4(self) -> int:
        digits = "".join(self._take() for _ in range(4))
        try:
            return int(digits, 16)
        except ValueError as exc:
            raise self._fail(f"invalid unicode escape {digits!r}") from exc

    def _parse_escape(self) -> str:
        code = self._take()
        simple = _ESCAPES.get(code)
        if simple is not None:
            return simple
        if code != "u":
            raise self._fail(f"invalid escape character {code!r}")
        value = self._hex4()
        if 0xD800 <= value <= 0xDBFF:
            if self._match("\\u"):
                low = self._hex4()
                if 0xDC00 <= low <= 0xDFFF:
                    return chr(0x10000 + ((value - 0xD800) << 10) + (low - 0xDC00))
            raise _issue(
                "invalid_unicode", "JSON contains an isolated Unicode surrogate code point"
            )
        if 0xDC00 <= value <= 0xDFFF:
            raise _issue(
                "invalid_unicode", "JSON contains an isolated Unicode surrogate code point"
            )
        return chr(value)

    def _parse_string(self, *, allow_large: bool) -> str | LargeValue:
        self._expect('"')
        collected: list[str] = []
        total = 0
        state: _LargeStringState | None = None
        for segment in self._string_segments():
            if state is not None:
                state.character_count += len(segment)
                self._absorb(state, segment)
                continue
            collected.append(segment)
            total += len(segment)
            if allow_large and total > self._large_threshold:
                state = _LargeStringState(digester=None)
                joined = "".join(collected)
                collected.clear()
                state.character_count = len(joined)
                self._absorb(state, joined)
        if state is None:
            text = "".join(collected)
            if len(text) > self.stats.peak_retained_characters:
                self.stats.peak_retained_characters = len(text)
            return text
        return self._finish_large(state)

    def _absorb(self, state: _LargeStringState, text: str) -> None:
        """Fold one slice of an over-long string into its measurements."""

        if not state.started:
            # The first slice is everything collected before the threshold was
            # crossed, and that threshold is above the retained prefix, so the
            # prefix is complete here and the payload's start is already known.
            state.prefix = text[:LARGE_VALUE_PREFIX_CHARACTERS]
            state.started = True
            body_start = self._begin_media(state)
            if body_start is None:
                return
            text = text[body_start:]
        if state.digester is None or not text:
            return
        try:
            state.digester.feed(text)
        except PayloadValidationError as exc:
            state.issue = _deferred(exc)
            state.digester = None

    def _begin_media(self, state: _LargeStringState) -> int | None:
        """Locate the Base64 payload and open a digester over it.

        A ``data:`` URL carries its header in the same string, and that header
        is bounded, so the comma ending it is always inside the retained prefix.
        """

        prefix = state.prefix
        body_start = 0
        if prefix[:5].lower() == "data:":
            comma = prefix.find(",", 5)
            if comma < 0 or comma - 5 > MAX_DATA_URL_HEADER_CHARACTERS:
                state.issue = DeferredIssue(
                    "data_url_header_too_long",
                    f"data URL header exceeds the {MAX_DATA_URL_HEADER_CHARACTERS}-character limit",
                )
                return None
            state.header = prefix[: comma + 1]
            body_start = comma + 1
        state.digester = Base64Digester(
            "$", self._media_max_bytes, allow_url_safe=self._allow_url_safe
        )
        return body_start

    def _finish_large(self, state: _LargeStringState) -> LargeValue:
        summary: Base64Summary | None = None
        if state.issue is None and state.digester is not None:
            try:
                summary = state.digester.finish()
            except PayloadValidationError as exc:
                state.issue = _deferred(exc)
        if summary is None and state.issue is None:  # pragma: no cover - defensive
            state.issue = DeferredIssue("invalid_base64", "Base64 payload could not be summarized")
        self.stats.large_values += 1
        if state.character_count > self.stats.largest_value_characters:
            self.stats.largest_value_characters = state.character_count
        if summary is not None:
            self.stats.streamed_media_bytes += summary.byte_length
        return LargeValue(
            character_count=state.character_count,
            prefix=state.prefix,
            data_url_header=state.header,
            media=summary,
            media_issue=state.issue,
        )


def _deferred(error: PayloadValidationError) -> DeferredIssue:
    issue = error.issues[0]
    return DeferredIssue(issue.code, issue.message, issue.hint)


def parse_json_stream(
    stream: IO[bytes],
    *,
    max_input_bytes: int,
    large_value_threshold: int,
    media_max_bytes: int,
    allow_url_safe_base64: bool = False,
) -> tuple[Any, StreamStatistics]:
    """Decode one JSON document from a byte stream under the strict contract.

    ``large_value_threshold`` is the length above which a string is kept as a
    :class:`LargeValue` instead of characters.  It must exceed
    :data:`LARGE_VALUE_PREFIX_CHARACTERS`, because every probe the pipeline
    makes into such a value reads the retained prefix.

    Returns the document and the statistics of the parse, so a caller can assert
    on what the run actually retained rather than trusting this docstring.
    """

    if type(max_input_bytes) is not int or max_input_bytes < 1:
        raise ValueError("max_input_bytes must be a positive integer")
    if type(large_value_threshold) is not int or large_value_threshold <= (
        LARGE_VALUE_PREFIX_CHARACTERS
    ):
        raise ValueError(
            f"large_value_threshold must be an integer greater than {LARGE_VALUE_PREFIX_CHARACTERS}"
        )
    if type(media_max_bytes) is not int or media_max_bytes < 0:
        raise ValueError("media_max_bytes must be a non-negative integer")
    parser = _Parser(
        _Source(stream, max_input_bytes),
        large_threshold=large_value_threshold,
        media_max_bytes=media_max_bytes,
        allow_url_safe=bool(allow_url_safe_base64),
    )
    document = parser.parse()
    return document, parser.stats


__all__ = [
    "CHUNK_BYTES",
    "LARGE_VALUE_PREFIX_CHARACTERS",
    "MAX_JSON_DEPTH",
    "MAX_JSON_INTEGER_DIGITS",
    "DeferredIssue",
    "LargeValue",
    "StreamStatistics",
    "parse_json_stream",
]
