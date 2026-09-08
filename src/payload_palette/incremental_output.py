"""Stateful UTF-8/JSON output parsing; semantic approval only at explicit EOF.

Complete-value events share immutable subtrees. No accumulated prefix is
reparsed, repaired or passed to a semantic validator. The full value graph is
retained under output limits; this is not constant-memory document validation.
"""

from __future__ import annotations

import codecs
import inspect
import math
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, NoReturn, TypeAlias, cast

from .output_schema import JSONScalar, JSONValue, OutputContractError, OutputPath, _text
from .output_validation import OutputReport, ValidationPipeline

ValueKind: TypeAlias = Literal["null", "boolean", "integer", "number", "string", "array", "object"]
_KINDS = {"null", "boolean", "integer", "number", "string", "array", "object"}
_WHITESPACE = frozenset(" \t\n\r")
_DELIMITERS = _WHITESPACE | frozenset(",]}")
_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
_INTEGER_MAX = 10**256 - 1
_BYTE_CEILING = 64_000_000
_WORK_CEILING = 256_000_000


def _integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")


@dataclass(frozen=True, slots=True)
class IncrementalLimits:
    """Additional parser/progress limits; these are not provider model-token budgets."""

    max_input_bytes: int = 4_000_000
    max_chunks: int = 100_000
    max_token_characters: int = 4_000_000
    max_tokens: int = 100_000
    max_work: int = 32_000_000

    def __post_init__(self) -> None:
        for name, maximum in (
            ("max_input_bytes", _BYTE_CEILING),
            ("max_chunks", 1_000_000),
            ("max_token_characters", _BYTE_CEILING),
            ("max_tokens", 1_000_000),
            ("max_work", _WORK_CEILING),
        ):
            _integer(getattr(self, name), name, 1, maximum)


class IncrementalStatus(StrEnum):
    OPEN = "open"
    FINISHED = "finished"
    FAILED = "failed"
    CLOSED = "closed"


class IncrementalJSONError(OutputContractError):
    """Stable parse/resource failure; does not include raw values or key text."""

    def __init__(self, code: str, character_offset: int) -> None:
        super().__init__(f"{code} at JSON character offset {character_offset}")
        self.code = code
        self.character_offset = character_offset


@dataclass(frozen=True, slots=True)
class JSONValueSnapshot:
    """An immutable complete JSON value; nested values are shared, never recopied.

    ``to_python`` explicitly makes a fresh mutable copy. Calling it repeatedly
    is caller work, not incremental parser work. Values may contain secrets.
    """

    kind: ValueKind
    scalar: JSONScalar = field(default=None, repr=False)
    items: tuple[JSONValueSnapshot, ...] = field(default=(), repr=False)
    properties: tuple[tuple[str, JSONValueSnapshot], ...] = field(default=(), repr=False)
    _nodes: int = field(init=False, repr=False)
    _characters: int = field(init=False, repr=False)
    _depth: int = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in _KINDS:
            raise ValueError("unknown JSON snapshot kind")
        if type(self.items) is not tuple or type(self.properties) is not tuple:
            raise ValueError("snapshot children must be immutable tuples")
        if len(self.items) > 100_000 or len(self.properties) > 100_000:
            raise ValueError("snapshot child count exceeds ceiling")
        if (self.kind != "array" and self.items) or (self.kind != "object" and self.properties):
            raise ValueError("snapshot children disagree with kind")
        children: tuple[JSONValueSnapshot, ...] = self.items
        characters = 0
        if self.kind == "object":
            if any(
                type(pair) is not tuple or len(pair) != 2 or not _text(pair[0])
                for pair in self.properties
            ):
                raise ValueError("snapshot properties require Unicode keys and values")
            if len({key for key, _ in self.properties}) != len(self.properties):
                raise ValueError("snapshot keys must be unique")
            children = tuple(value for _, value in self.properties)
            characters = sum(len(key) for key, _ in self.properties)
        if any(type(child) is not JSONValueSnapshot for child in children):
            raise ValueError("snapshot children must be JSONValueSnapshot")
        scalar_valid = {
            "null": self.scalar is None,
            "boolean": type(self.scalar) is bool,
            "integer": type(self.scalar) is int and abs(self.scalar) <= _INTEGER_MAX,
            "number": type(self.scalar) is float and math.isfinite(self.scalar),
            "string": _text(self.scalar),
            "array": self.scalar is None,
            "object": self.scalar is None,
        }[self.kind]
        if not scalar_valid:
            raise ValueError("snapshot scalar disagrees with kind or JSON range")
        if self.kind == "string":
            characters += len(cast(str, self.scalar))
        nodes = 1 + sum(child._nodes for child in children)
        characters += sum(child._characters for child in children)
        depth = max((1 + child._depth for child in children), default=0)
        if nodes > 100_000 or characters > 8_000_000 or depth > 64:
            raise ValueError("snapshot exceeds compiled JSON graph ceilings")
        object.__setattr__(self, "_nodes", nodes)
        object.__setattr__(self, "_characters", characters)
        object.__setattr__(self, "_depth", depth)

    def to_python(self) -> JSONValue:
        if self.kind == "object":
            return {key: child.to_python() for key, child in self.properties}
        if self.kind == "array":
            return [child.to_python() for child in self.items]
        return self.scalar


@dataclass(frozen=True, slots=True)
class CompletedJSONValue:
    """Provisional syntax completion, not schema validity or semantic approval."""

    index: int
    path: OutputPath
    end_character: int
    value: JSONValueSnapshot = field(repr=False)

    def __post_init__(self) -> None:
        _integer(self.index, "event index", 0, 99_999)
        _integer(self.end_character, "end_character", 0, _BYTE_CEILING)
        if (
            type(self.path) is not tuple
            or len(self.path) > 64
            or sum(len(part) for part in self.path if type(part) is str) > 8_000_000
            or any(
                not (_text(part) or (type(part) is int and 0 <= part < 100_000))
                for part in self.path
            )
        ):
            raise ValueError("event path must be a bounded immutable JSON path")
        if type(self.value) is not JSONValueSnapshot:
            raise ValueError("event value must be an immutable JSONValueSnapshot")


@dataclass(frozen=True, slots=True)
class IncrementalStatistics:
    status: IncrementalStatus
    bytes_received: int
    chunks_received: int
    characters_consumed: int
    nodes_started: int
    values_completed: int
    decoded_string_characters: int
    tokens: int
    work: int
    root_complete: bool

    def __post_init__(self) -> None:
        if type(self.status) is not IncrementalStatus or type(self.root_complete) is not bool:
            raise ValueError("statistics require a status and boolean root flag")
        for name, maximum in (
            ("bytes_received", _BYTE_CEILING),
            ("chunks_received", 1_000_000),
            ("characters_consumed", _BYTE_CEILING),
            ("nodes_started", 100_000),
            ("values_completed", 100_000),
            ("decoded_string_characters", 8_000_000),
            ("tokens", 1_000_000),
            ("work", _WORK_CEILING),
        ):
            _integer(getattr(self, name), name, 0, maximum)
        if (
            self.values_completed > self.nodes_started
            or self.characters_consumed > self.bytes_received
        ):
            raise ValueError("statistics counters contradict one another")
        if self.root_complete and (
            not self.nodes_started or self.values_completed != self.nodes_started
        ):
            raise ValueError("a complete root requires every started node to be complete")
        if self.status is IncrementalStatus.FINISHED and not self.root_complete:
            raise ValueError("a finished document must have a complete root")


@dataclass(frozen=True, slots=True)
class IncrementalProgress:
    events: tuple[CompletedJSONValue, ...]
    statistics: IncrementalStatistics

    def __post_init__(self) -> None:
        _check_events(self.events, self.statistics, IncrementalStatus.OPEN)


@dataclass(frozen=True, slots=True)
class IncrementalResult:
    report: OutputReport
    final_events: tuple[CompletedJSONValue, ...]
    statistics: IncrementalStatistics

    def __post_init__(self) -> None:
        if type(self.report) is not OutputReport:
            raise ValueError("result requires a complete OutputReport")
        _check_events(self.final_events, self.statistics, IncrementalStatus.FINISHED)


def _check_events(
    events: tuple[CompletedJSONValue, ...], stats: IncrementalStatistics, status: IncrementalStatus
) -> None:
    if type(stats) is not IncrementalStatistics or stats.status is not status:
        raise ValueError("event batch statistics have an incompatible status")
    if (
        type(events) is not tuple
        or len(events) > stats.values_completed
        or any(type(event) is not CompletedJSONValue for event in events)
    ):
        raise ValueError("events must be an immutable bounded event tuple")
    for position, event in enumerate(events):
        if event.index >= stats.values_completed or event.end_character > stats.characters_consumed:
            raise ValueError("event exceeds its batch counters")
        if position and (
            event.index != events[position - 1].index + 1
            or event.end_character < events[position - 1].end_character
        ):
            raise ValueError("events must be contiguous and ordered")


@dataclass(slots=True)
class _Container:
    kind: Literal["array", "object"]
    path: OutputPath
    state: str = "first"
    items: list[JSONValueSnapshot] = field(default_factory=list)
    properties: dict[str, JSONValueSnapshot] = field(default_factory=dict)
    key: str | None = None


class IncrementalOutputSession:
    """Single-owner push parser. Explicit finish is required even after root completion."""

    def __init__(
        self, pipeline: ValidationPipeline, limits: IncrementalLimits | None = None
    ) -> None:
        if type(pipeline) is not ValidationPipeline:
            raise ValueError("pipeline must be ValidationPipeline")
        self._pipeline = pipeline
        self._limits = IncrementalLimits() if limits is None else limits
        if type(self._limits) is not IncrementalLimits:
            raise ValueError("limits must be IncrementalLimits")
        self._owner = threading.get_ident()
        self._busy = False
        self._status = IncrementalStatus.OPEN
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        self._stack: list[_Container] = []
        self._root: JSONValueSnapshot | None = None
        self._root_complete = False
        self._events: list[CompletedJSONValue] = []
        self._bytes = self._chunks = self._offset = self._nodes = self._completed = 0
        self._characters = self._tokens = self._work = 0
        self._mode = ""
        self._lexical_state = ""
        self._parts: list[str] = []
        self._token_length = 0
        self._token_path: OutputPath = ()
        self._key_token = False
        self._literal = ""
        self._hex_count = self._hex_value = self._high_surrogate = 0

    @property
    def pipeline(self) -> ValidationPipeline:
        return self._pipeline

    @property
    def limits(self) -> IncrementalLimits:
        return self._limits

    @property
    def statistics(self) -> IncrementalStatistics:
        return IncrementalStatistics(
            self._status,
            self._bytes,
            self._chunks,
            self._offset,
            self._nodes,
            self._completed,
            self._characters,
            self._tokens,
            self._work,
            self._root_complete,
        )

    def _check(self, *, require_open: bool = True) -> None:
        if threading.get_ident() != self._owner:
            raise RuntimeError("incremental output sessions are single-owner")
        if self._busy:
            raise RuntimeError("incremental output sessions are not reentrant")
        if require_open and self._status is not IncrementalStatus.OPEN:
            raise RuntimeError("incremental output session is terminal")

    def _fail(self, code: str) -> NoReturn:
        raise IncrementalJSONError(code, self._offset)

    def _charge(self, amount: int = 1) -> None:
        if amount > self.limits.max_work - self._work:
            self._fail("work_limit")
        self._work += amount

    def _token(self) -> None:
        if self._tokens >= self.limits.max_tokens:
            self._fail("token_limit")
        self._tokens += 1

    def _clear(self) -> None:
        self._root = None
        self._stack.clear()
        self._parts.clear()
        self._events.clear()
        self._mode = ""
        self._token_path = ()
        self._decoder.reset()

    def close(self) -> None:
        """Release retained parse values; a failed status remains failed."""
        self._check(require_open=False)
        if self._status is IncrementalStatus.OPEN:
            self._status = IncrementalStatus.CLOSED
        self._clear()

    def feed(self, chunk: bytes) -> IncrementalProgress:
        """Consume exact bytes once. Empty chunks are not EOF and still cost work."""
        self._check()
        self._busy = True
        try:
            if type(chunk) is not bytes:
                self._fail("chunk_type")
            if self._chunks >= self.limits.max_chunks:
                self._fail("chunk_limit")
            if len(chunk) > self.limits.max_input_bytes - self._bytes:
                self._fail("byte_limit")
            self._charge()
            self._chunks += 1
            self._bytes += len(chunk)
            try:
                text = self._decoder.decode(chunk, final=False)
            except UnicodeError as exc:
                raise IncrementalJSONError("invalid_utf8", self._offset) from exc
            for character in text:
                self._offset += 1
                self._consume(character)
            events = tuple(self._events)
            self._events.clear()
            return IncrementalProgress(events, self.statistics)
        except BaseException:
            self._status = IncrementalStatus.FAILED
            self._clear()
            raise
        finally:
            self._busy = False

    def finish(self) -> IncrementalResult:
        """Assert actual EOF, then run complete schema/semantic validation once."""
        self._check()
        self._busy = True
        try:
            self._charge()
            try:
                self._decoder.decode(b"", final=True)
            except UnicodeError as exc:
                raise IncrementalJSONError("incomplete_utf8", self._offset) from exc
            if self._mode == "number" and self._lexical_state in {
                "zero",
                "integer",
                "fraction",
                "exponent",
            }:
                self._number_done(self._offset)
            if self._mode or self._stack or not self._root_complete:
                self._fail("incomplete_json")
            root = self._root
            if root is None:  # Internal representation always wraps even a JSON null.
                raise RuntimeError("complete root snapshot is unavailable")
            report = self.pipeline.validate(root.to_python())
            self._status = IncrementalStatus.FINISHED
            result = IncrementalResult(report, tuple(self._events), self.statistics)
            self._clear()
            return result
        except BaseException:
            self._status = IncrementalStatus.FAILED
            self._clear()
            raise
        finally:
            self._busy = False

    def _path(self) -> OutputPath:
        if not self._stack:
            return ()
        parent = self._stack[-1]
        if parent.kind == "array":
            return (*parent.path, len(parent.items))
        if parent.key is None:
            raise RuntimeError("object value has no key")
        return (*parent.path, parent.key)

    def _start_value(self, character: str) -> None:
        path = self._path()
        if len(path) > self.pipeline.limits.max_depth:
            self._fail("depth_limit")
        if self._nodes >= self.pipeline.limits.max_nodes:
            self._fail("node_limit")
        self._nodes += 1
        self._token()
        if character in "[{":
            self._stack.append(_Container("array" if character == "[" else "object", path))
        elif character == '"':
            self._start_string(path, key=False)
        elif character == "-" or character in "0123456789":
            self._mode, self._token_path = "number", path
            self._lexical_state = (
                "minus" if character == "-" else "zero" if character == "0" else "integer"
            )
            self._parts = [character]
            self._token_length = 1
        elif character in "tfn":
            self._mode, self._token_path = "literal", path
            self._literal = {"t": "true", "f": "false", "n": "null"}[character]
            self._token_length = 1
        else:
            self._fail("invalid_json")

    def _start_string(self, path: OutputPath, *, key: bool) -> None:
        self._mode, self._lexical_state = "string", "body"
        self._token_path, self._key_token = path, key
        self._parts = []
        self._token_length = 1

    def _complete(self, value: JSONValueSnapshot, path: OutputPath, end: int) -> None:
        self._charge(1 + len(path) + sum(len(part) for part in path if type(part) is str))
        self._events.append(CompletedJSONValue(self._completed, path, end, value))
        self._completed += 1
        if self._stack:
            parent = self._stack[-1]
            if parent.kind == "array":
                parent.items.append(value)
            else:
                if parent.key is None:
                    raise RuntimeError("object completion has no key")
                parent.properties[parent.key] = value
                parent.key = None
            parent.state = "comma"
        else:
            self._root, self._root_complete = value, True

    def _consume(self, character: str) -> None:
        while True:
            self._charge()
            if self._mode:
                if self._mode == "number" and self._number_character(character):
                    continue  # The delimiter was not consumed; parse it once as syntax.
                if self._mode == "string":
                    self._string_character(character)
                elif self._mode == "literal":
                    self._literal_character(character)
                return
            if character in _WHITESPACE:
                return
            if not self._stack:
                if self._root_complete:
                    self._fail("trailing_content")
                self._start_value(character)
                return
            parent = self._stack[-1]
            close = "]" if parent.kind == "array" else "}"
            if character == close and parent.state in {"first", "comma"}:
                self._token()
                self._stack.pop()
                self._charge(len(parent.items) + len(parent.properties))
                value = JSONValueSnapshot(
                    parent.kind,
                    items=tuple(parent.items),
                    properties=tuple(parent.properties.items()),
                )
                self._complete(value, parent.path, self._offset)
                return
            if parent.state == "comma":
                if character != ",":
                    self._fail("invalid_json")
                self._token()
                parent.state = "key" if parent.kind == "object" else "value"
                return
            if parent.kind == "object" and parent.state in {"first", "key"}:
                if character != '"':
                    self._fail("invalid_json")
                self._token()
                self._start_string(parent.path, key=True)
                return
            if parent.state == "colon":
                if character != ":":
                    self._fail("invalid_json")
                self._token()
                parent.state = "value"
                return
            self._start_value(character)
            return

    def _token_character(self) -> None:
        if self._token_length >= self.limits.max_token_characters:
            self._fail("token_character_limit")
        self._token_length += 1

    def _literal_character(self, character: str) -> None:
        self._token_character()
        if character != self._literal[self._token_length - 1]:
            self._fail("invalid_json")
        if self._token_length == len(self._literal):
            literal = self._literal
            self._mode = ""
            self._complete(
                JSONValueSnapshot(
                    "null" if literal == "null" else "boolean",
                    {"true": True, "false": False, "null": None}[literal],
                ),
                self._token_path,
                self._offset,
            )

    def _number_character(self, character: str) -> bool:
        state = self._lexical_state
        digit = character in "0123456789"
        if state in {"minus", "fraction_start", "exponent_start", "exponent_sign"}:
            if state == "exponent_start" and character in "+-":
                new = "exponent_sign"
            elif digit:
                new = (
                    ("zero" if character == "0" else "integer")
                    if state == "minus"
                    else "fraction"
                    if state == "fraction_start"
                    else "exponent"
                )
            else:
                self._fail("invalid_json")
        elif digit:
            if state == "zero":
                self._fail("invalid_json")
            new = state
        elif character == "." and state in {"zero", "integer"}:
            new = "fraction_start"
        elif character in "eE" and state in {"zero", "integer", "fraction"}:
            new = "exponent_start"
        elif character in _DELIMITERS:
            self._number_done(self._offset - 1)
            return True
        else:
            self._fail("invalid_json")
        self._token_character()
        self._lexical_state = new
        self._parts.append(character)
        return False

    def _number_done(self, end: int) -> None:
        literal = "".join(self._parts)
        if "." in literal or "e" in literal or "E" in literal:
            number = float(literal)
            if not math.isfinite(number):
                self._fail("json_number_range")
            value = JSONValueSnapshot("number", number)
        else:
            if len(literal) - literal.startswith("-") > 256:
                self._fail("json_number_range")
            value = JSONValueSnapshot("integer", int(literal))
        self._parts.clear()
        self._mode = ""
        self._complete(value, self._token_path, end)

    def _append_string(self, character: str) -> None:
        if self._characters >= self.pipeline.limits.max_characters:
            self._fail("character_limit")
        self._characters += 1
        self._parts.append(character)

    def _string_character(self, character: str) -> None:
        self._token_character()
        state = self._lexical_state
        if state == "body":
            if character == '"':
                text = "".join(self._parts)
                self._parts.clear()
                self._mode = ""
                if self._key_token:
                    parent = self._stack[-1]
                    if text in parent.properties:
                        self._fail("duplicate_json_key")
                    parent.key, parent.state = text, "colon"
                else:
                    self._complete(
                        JSONValueSnapshot("string", text), self._token_path, self._offset
                    )
            elif character == "\\":
                self._lexical_state = "escape"
            elif ord(character) < 32:
                self._fail("invalid_json")
            else:
                self._append_string(character)
        elif state == "escape":
            if character in _ESCAPES:
                self._append_string(_ESCAPES[character])
                self._lexical_state = "body"
            elif character == "u":
                self._hex_value = self._hex_count = 0
                self._lexical_state = "hex"
            else:
                self._fail("invalid_json")
        elif state in {"surrogate_backslash", "surrogate_u"}:
            expected = "\\" if state == "surrogate_backslash" else "u"
            if character != expected:
                self._fail("invalid_unicode")
            if state == "surrogate_backslash":
                self._lexical_state = "surrogate_u"
            else:
                self._hex_value = self._hex_count = 0
                self._lexical_state = "low_hex"
        else:
            if character not in "0123456789abcdefABCDEF":
                self._fail("invalid_json")
            self._hex_value = self._hex_value * 16 + int(character, 16)
            self._hex_count += 1
            if self._hex_count == 4:
                code = self._hex_value
                if state == "low_hex":
                    if not 0xDC00 <= code <= 0xDFFF:
                        self._fail("invalid_unicode")
                    self._append_string(
                        chr(0x10000 + ((self._high_surrogate - 0xD800) << 10) + code - 0xDC00)
                    )
                    self._lexical_state = "body"
                elif 0xD800 <= code <= 0xDBFF:
                    self._high_surrogate, self._lexical_state = code, "surrogate_backslash"
                elif 0xDC00 <= code <= 0xDFFF:
                    self._fail("invalid_unicode")
                else:
                    self._append_string(chr(code))
                    self._lexical_state = "body"


@contextmanager
def _owned_iterator(iterator: Iterator[bytes], close: bool) -> Iterator[Iterator[bytes]]:
    primary: BaseException | None = None
    try:
        yield iterator
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if close:
            try:
                method = getattr(iterator, "close", None)
                if method is not None:
                    returned = method()
                    if inspect.isawaitable(returned) or inspect.isasyncgen(returned):
                        if inspect.iscoroutine(returned):
                            with suppress(Exception):
                                returned.close()
                        raise ValueError("iterator.close must be synchronous")
            except Exception:
                if primary is None:
                    raise
                primary.add_note("incremental output iterator cleanup also failed")


def validate_output_chunks(
    chunks: Iterable[bytes],
    pipeline: ValidationPipeline,
    *,
    limits: IncrementalLimits | None = None,
    on_progress: Callable[[IncrementalProgress], None] | None = None,
    close_iterator: bool = False,
) -> IncrementalResult:
    """Consume a pull iterable; close its iterator only with explicit ownership.

    A progress observer sees provisional actual values/keys, not redacted
    generation history. It must be synchronous and return None. Owned cleanup
    must succeed before semantic validation/approval can begin.
    """
    if type(close_iterator) is not bool:
        raise ValueError("close_iterator must be a boolean")
    if on_progress is not None and (
        not callable(on_progress)
        or inspect.iscoroutinefunction(on_progress)
        or inspect.iscoroutinefunction(getattr(on_progress, "__call__", None))  # noqa: B004
    ):
        raise ValueError("on_progress must be synchronous")
    session = IncrementalOutputSession(pipeline, limits)
    try:
        with _owned_iterator(iter(chunks), close_iterator) as iterator:
            for chunk in iterator:
                progress = session.feed(chunk)
                if on_progress is not None:
                    returned = cast(Callable[[IncrementalProgress], object], on_progress)(progress)
                    if returned is not None:
                        if inspect.iscoroutine(returned):
                            with suppress(Exception):
                                returned.close()
                        raise ValueError("on_progress must return None")
        return session.finish()
    except BaseException:
        session.close()
        raise


__all__ = [
    "CompletedJSONValue",
    "IncrementalJSONError",
    "IncrementalLimits",
    "IncrementalOutputSession",
    "IncrementalProgress",
    "IncrementalResult",
    "IncrementalStatistics",
    "IncrementalStatus",
    "JSONValueSnapshot",
    "validate_output_chunks",
]
