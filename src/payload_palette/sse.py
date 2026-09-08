"""Bounded offline SSE framing, separate from JSON approval and HTTP transport."""

from __future__ import annotations

import codecs
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, NoReturn

from .output_schema import OutputContractError

_RETRY_MAX = 2**63 - 1
_RETRY_TEXT = str(_RETRY_MAX)


def _integer(value: object, name: str, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")


def _scalar(value: object, maximum: int) -> bool:
    return (
        type(value) is str
        and len(value) <= maximum
        and not any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    )


def _check_utf8_mode(value: object) -> None:
    if type(value) is not str or value not in ("replace", "strict"):
        raise ValueError("utf8_errors must be replace or strict")


@dataclass(frozen=True, slots=True)
class SSELimits:
    """Finite framing budgets, not a browser/network or hard-RSS sandbox."""

    max_input_bytes: int = 4_000_000
    max_chunks: int = 100_000
    max_line_characters: int = 262_144
    max_data_characters: int = 1_000_000
    max_data_fields: int = 10_000
    max_metadata_characters: int = 1_024
    max_lines: int = 200_000
    max_events: int = 10_000
    max_output_characters: int = 8_000_000
    max_work: int = 32_000_000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_input_bytes", 64_000_000),
            ("max_chunks", 1_000_000),
            ("max_line_characters", 4_000_000),
            ("max_data_characters", 8_000_000),
            ("max_data_fields", 100_000),
            ("max_metadata_characters", 16_384),
            ("max_lines", 2_000_000),
            ("max_events", 100_000),
            ("max_output_characters", 64_000_000),
            ("max_work", 256_000_000),
        ):
            _integer(getattr(self, name), name, maximum)


class SSEStatus(StrEnum):
    OPEN = "open"
    FINISHED = "finished"
    FAILED = "failed"
    CLOSED = "closed"


class SSEDecodeError(OutputContractError):
    """Stable error code/decoded-character offset; no raw line or payload text."""

    def __init__(self, code: str, character_offset: int) -> None:
        super().__init__(f"{code} at SSE character offset {character_offset}")
        self.code = code
        self.character_offset = character_offset


@dataclass(frozen=True, slots=True)
class SSEEvent:
    """Complete framing message, not semantic approval. Values can contain secrets."""

    sequence: int
    event_type: str = field(repr=False)
    data: str = field(repr=False)
    last_event_id: str = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or not 0 <= self.sequence < 100_000:
            raise ValueError("SSE sequence must be an integer in 0..99999")
        if not _scalar(self.data, 8_000_000):
            raise ValueError("SSE data must be bounded Unicode scalar text")
        for name in ("event_type", "last_event_id"):
            value = getattr(self, name)
            if not _scalar(value, 16_384) or "\r" in value or "\n" in value:
                raise ValueError("SSE metadata must be bounded single-line scalar text")
        if not self.event_type or "\x00" in self.last_event_id:
            raise ValueError("SSE event type must be nonempty and ID must exclude NUL")


@dataclass(frozen=True, slots=True)
class SSESnapshot:
    status: SSEStatus
    input_bytes: int
    chunks: int
    characters: int
    lines: int
    events: int
    output_characters: int
    work: int
    last_event_id: str = field(repr=False)
    retry_milliseconds: int | None
    discarded_unterminated_line: bool
    discarded_pending_block: bool


@dataclass(frozen=True, slots=True)
class SSEBatch:
    events: tuple[SSEEvent, ...]
    snapshot: SSESnapshot


class SSEDecoder:
    """Single-owner finite stream decoder; explicit EOF never synthesizes a message.

    Failed feed calls return no batch even if earlier messages in that call were
    parsed. Internal counters do not roll back; the failed decoder is terminal.
    Previously returned messages remain complete SSE messages, not whole-stream
    success or JSON/provider approval. No source, task or connection is owned.
    """

    def __init__(
        self,
        limits: SSELimits | None = None,
        *,
        utf8_errors: Literal["replace", "strict"] = "replace",
    ) -> None:
        if limits is not None and type(limits) is not SSELimits:
            raise ValueError("limits must be SSELimits")
        _check_utf8_mode(utf8_errors)
        self._limits = limits if limits is not None else SSELimits()
        # utf-8-sig can swallow a partial BOM at EOF on supported interpreters.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors=utf8_errors)
        self._owner = threading.current_thread()
        self._busy = False
        self._status = SSEStatus.OPEN
        self._first = True
        self._after_cr = False
        self._line: list[str] = []
        self._data: list[str] = []
        self._data_size = 0
        self._event_type = ""
        self._id_buffer = self._last_id = ""
        self._retry: int | None = None
        self._block = False
        self._discarded_line = self._discarded_block = False
        self._bytes = self._chunks = self._characters = self._lines = 0
        self._events = self._output_characters = self._work = 0

    @property
    def limits(self) -> SSELimits:
        return self._limits

    @property
    def snapshot(self) -> SSESnapshot:
        return SSESnapshot(
            self._status,
            self._bytes,
            self._chunks,
            self._characters,
            self._lines,
            self._events,
            self._output_characters,
            self._work,
            self._last_id,
            self._retry,
            self._discarded_line,
            self._discarded_block,
        )

    def _check(self, *, require_open: bool = True) -> None:
        if threading.current_thread() is not self._owner:
            raise RuntimeError("SSE decoders are single-owner")
        if self._busy:
            raise RuntimeError("SSE decoders are not reentrant")
        if require_open and self._status is not SSEStatus.OPEN:
            raise RuntimeError("SSE decoder is terminal")

    def _fail(self, code: str) -> NoReturn:
        raise SSEDecodeError(code, self._characters)

    def _charge(self, amount: int) -> None:
        if amount > self.limits.max_work - self._work:
            self._fail("work_limit")
        self._work += amount

    def _clear(self) -> None:
        self._line.clear()
        self._data.clear()
        self._data_size = 0
        self._event_type = ""
        self._id_buffer = self._last_id
        self._block = self._after_cr = False
        self._decoder.reset()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        self._check()
        self._busy = True
        try:
            yield
        except BaseException:
            self._status = SSEStatus.FAILED
            self._clear()
            raise
        finally:
            self._busy = False

    def close(self) -> None:
        """Release pending data; keep terminal status and committed control metadata."""
        self._check(require_open=False)
        if self._status is SSEStatus.OPEN:
            self._status = SSEStatus.CLOSED
        self._clear()

    def feed(self, chunk: bytes) -> SSEBatch:
        """Admit one exact bytes chunk. Empty chunks cost work and are not EOF."""
        with self._operation():
            if type(chunk) is not bytes:
                self._fail("chunk_type")
            if self._chunks >= self.limits.max_chunks:
                self._fail("chunk_limit")
            if len(chunk) > self.limits.max_input_bytes - self._bytes:
                self._fail("byte_limit")
            self._charge(1)
            self._chunks += 1
            self._bytes += len(chunk)
            events: list[SSEEvent] = []
            self._decode(chunk, events, final=False)
            return SSEBatch(tuple(events), self.snapshot)

    def finish(self) -> SSEBatch:
        """Flush UTF-8 then discard unterminated lines/blocks, without dispatch."""
        with self._operation():
            self._charge(1)
            self._decode(b"", [], final=True)
            self._discarded_line = bool(self._line)
            self._discarded_block = self._block
            self._clear()
            self._status = SSEStatus.FINISHED
            return SSEBatch((), self.snapshot)

    def _decode(self, chunk: bytes, events: list[SSEEvent], *, final: bool) -> None:
        try:
            text = self._decoder.decode(chunk, final=final)
        except UnicodeError as error:
            raise SSEDecodeError("invalid_utf8", self._characters) from error
        for char in text:
            self._charge(1)
            self._characters += 1
            if self._first:
                self._first = False
                if char == "\ufeff":
                    continue
            if self._after_cr:
                self._after_cr = False
                if char == "\n":
                    continue
            if char in ("\r", "\n"):
                self._end_line(events)
                self._after_cr = char == "\r"
            else:
                if len(self._line) >= self.limits.max_line_characters:
                    self._fail("line_limit")
                self._line.append(char)
                self._block = True

    def _end_line(self, events: list[SSEEvent]) -> None:
        if self._lines >= self.limits.max_lines:
            self._fail("line_count_limit")
        # Join, delimiter search/slicing and field checks have linear line cost.
        self._charge(1 + 4 * len(self._line))
        self._lines += 1
        line = "".join(self._line)
        self._line.clear()
        if not line:
            self._dispatch(events)
            self._block = False
            return
        if line.startswith(":"):
            return
        name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if name == "data":
            if len(self._data) >= self.limits.max_data_fields:
                self._fail("data_field_limit")
            if len(value) + 1 > self.limits.max_data_characters - self._data_size:
                self._fail("data_limit")
            self._data.append(value)
            self._data_size += len(value) + 1
        elif name in ("id", "event"):
            if name == "id" and "\x00" in value:
                return
            if len(value) > self.limits.max_metadata_characters:
                self._fail("metadata_limit")
            if name == "id":
                self._id_buffer = value
            else:
                self._event_type = value
        elif name == "retry" and value and value.isascii() and value.isdecimal():
            significant = value.lstrip("0") or "0"
            if len(significant) > len(_RETRY_TEXT) or (
                len(significant) == len(_RETRY_TEXT) and significant > _RETRY_TEXT
            ):
                self._fail("retry_limit")
            self._retry = int(significant)

    def _dispatch(self, events: list[SSEEvent]) -> None:
        self._last_id = self._id_buffer
        if self._data:
            if self._events >= self.limits.max_events:
                self._fail("event_limit")
            event_type = self._event_type or "message"
            size = self._data_size - 1 + len(event_type) + len(self._last_id)
            if size > self.limits.max_output_characters - self._output_characters:
                self._fail("output_limit")
            # Charge before joining/constructor scans; repeated IDs cost each time.
            self._charge(1 + 4 * size)
            data = "\n".join(self._data)
            event = SSEEvent(self._events, event_type, data, self._last_id)
            events.append(event)
            self._events += 1
            self._output_characters += size
        self._data.clear()
        self._data_size = 0
        self._event_type = ""


__all__ = [
    "SSEBatch",
    "SSEDecodeError",
    "SSEDecoder",
    "SSEEvent",
    "SSELimits",
    "SSESnapshot",
    "SSEStatus",
]
