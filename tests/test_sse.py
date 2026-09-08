from __future__ import annotations

import dataclasses
import itertools
import random
import re
import runpy
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import payload_palette.sse as framing
from payload_palette.sse import SSEDecodeError, SSEDecoder, SSEEvent, SSELimits, SSEStatus


def event_rows(events):
    return [(v.sequence, v.event_type, v.data, v.last_event_id) for v in events]


def whole_oracle(raw):
    # Whole-body reference: normalize only the three SSE delimiters, not splitlines().
    text = raw.decode("utf-8", errors="replace").removeprefix("\ufeff")
    lines = re.split(r"\r\n|\r|\n", text)
    unterminated = bool(lines.pop())
    data = []
    kind = pending_id = committed_id = ""
    retry = None
    rows = []
    pending = unterminated
    for line in lines:
        if line == "":
            committed_id = pending_id
            if data:
                rows.append((len(rows), kind or "message", "\n".join(data), committed_id))
            data = []
            kind = ""
            pending = False
        else:
            pending = True
            if line[0] == ":":
                continue
            pair = line.split(":", 1)
            name = pair[0]
            value = pair[1] if len(pair) == 2 else ""
            value = value[1:] if value.startswith(" ") else value
            if name == "data":
                data.append(value)
            elif name == "event":
                kind = value
            elif name == "id" and "\0" not in value:
                pending_id = value
            elif name == "retry" and re.fullmatch("[0-9]+", value):
                retry = int(value)
    return rows, committed_id, retry, unterminated, pending or unterminated


VECTORS = [
    b"",
    b"\n",
    b"\r",
    b"\r\n",
    b"data\n\n",
    b"data:\r\r",
    b"data: first\r\ndata: second\r\n\r\n",
    b": comment\nignored\n\n",
    b"event: custom\nid: 31\ndata: x\n\ndata: y\n\n",
    b"id: retained\n\nid: incomplete\ndata: lost\n",
    b"id: x\n\ndata: a\n\nid\n\ndata: b\n\n",
    b"id: okay\nid: bad\0value\ndata: a\n\n",
    b"event: forgotten\n\ndata: default\n\n",
    b"data:  two\ndata:\ttab\ndata:a:b\ndata\n\n",
    b"DATA: no\nData: no\n data: no\ndata: yes\n\n",
    b"retry: 0012\nretry: -2\nretry: +1\nretry: 1.5\nretry: \nretry: 0\n",
    b"retry: 81\nretry: 45",
    b"data: [DONE]\n\ndata: more\n\n",
    b"\xef\xbb\xbfdata: BOM\n\n",
    b"\xef\xbb\xbf\xef\xbb\xbfdata: no\n\n",
    b"data: \xef\xbb\xbfkept\n\n",
    b"\xef",
    b"\xef\xbb",
    b"data: \xff\xc0\xed\xa0\x80\xf0\x80\x80\x80\n\n",
    "data: snow雪🙂\u2028not-a-line\u0085nor-this\n\n".encode(),
    "retry: \uff11\uff12\ndata: \0\nevent: \0\n\n".encode(),
    b"data: lost",
    b"data: lost\n",
    b"data: yes\r\n\r\n: pending",
    b"data: a\r\n\rdata: b\n\n",
    b"id: a\r\rdata:\r\r\n",
]


@pytest.mark.parametrize("raw", VECTORS)
def test_every_split_against_independent_whole_body_oracle(raw):
    expected = whole_oracle(raw)
    for cut in range(len(raw) + 1):
        parser = SSEDecoder()
        events = parser.feed(raw[:cut]).events + parser.feed(raw[cut:]).events
        final = parser.finish()
        assert final.events == ()
        assert (
            event_rows(events),
            final.snapshot.last_event_id,
            final.snapshot.retry_milliseconds,
            final.snapshot.discarded_unterminated_line,
            final.snapshot.discarded_pending_block,
        ) == expected
        assert final.snapshot.status is SSEStatus.FINISHED
        assert final.snapshot.input_bytes == len(raw)


def test_all_three_splits_and_bytewise_unicode_crlf():
    raw = "\ufeffdata: 雪🙂\r\n\r\n".encode()
    for a, b in itertools.combinations_with_replacement(range(len(raw) + 1), 2):
        parser = SSEDecoder()
        rows = []
        for part in (raw[:a], raw[a:b], raw[b:]):
            rows.extend(parser.feed(part).events)
        assert event_rows(rows) == [(0, "message", "雪🙂", "")]
        assert parser.finish().snapshot.discarded_pending_block is False
    parser = SSEDecoder()
    events = [event for byte in raw for event in parser.feed(bytes([byte])).events]
    assert event_rows(events) == [(0, "message", "雪🙂", "")]


def test_seeded_malformed_unicode_and_arbitrary_chunking_oracle():
    rng = random.Random(81451)
    alphabet = [b"a", b":", b" ", b"\t", b"\x00", b"\xff", b"\xef\xbb", "雪🙂".encode()]
    for _ in range(160):
        raw = b"".join(
            rng.choice((b"data:", b"id:", b"event:", b":", b"unknown:"))
            + b"".join(rng.choice(alphabet) for _ in range(rng.randrange(6)))
            + rng.choice((b"\r", b"\r\n", b"\n"))
            + rng.choice((b"", b"\n"))
            for _ in range(rng.randrange(1, 14))
        )
        parser = SSEDecoder()
        events = []
        start = 0
        while start < len(raw):
            end = start + rng.randrange(1, 9)
            events.extend(parser.feed(raw[start:end]).events)
            start = end
        final = parser.finish().snapshot
        assert (
            event_rows(events),
            final.last_event_id,
            final.retry_milliseconds,
            final.discarded_unterminated_line,
            final.discarded_pending_block,
        ) == whole_oracle(raw)


@pytest.mark.parametrize(
    "raw", [b"\xff", b"\xef", b"\xef\xbb", b"\xed\xa0\x80", b"\xf4\x90\x80\x80"]
)
def test_strict_utf8_and_partial_bom_fail_without_payload_in_error(raw):
    parser = SSEDecoder(utf8_errors="strict")
    with pytest.raises(SSEDecodeError, match="invalid_utf8") as error:
        parser.feed(raw)
        parser.finish()
    assert error.value.code == "invalid_utf8"
    assert parser.snapshot.status is SSEStatus.FAILED
    assert parser._line == []
    with pytest.raises(RuntimeError, match="terminal"):
        parser.feed(b"data: x\n\n")


@pytest.mark.parametrize("raw", [b"\xef", b"\xef\xbb"])
def test_partial_bom_replaced_at_eof_not_silently_swallowed(raw):
    parser = SSEDecoder()
    assert parser.feed(raw).snapshot.characters == 0
    final = parser.finish().snapshot
    assert final.characters == 1
    assert final.discarded_unterminated_line and final.discarded_pending_block


def test_control_metadata_commit_boundaries_and_event_type_reset():
    parser = SSEDecoder()
    assert parser.feed(b"id: committed\n\n").events == ()
    assert parser.snapshot.last_event_id == "committed"
    assert parser.feed(b"id: pending\nretry: 123\n").events == ()
    assert parser.snapshot.last_event_id == "committed"
    assert parser.snapshot.retry_milliseconds == 123
    final = parser.finish().snapshot
    assert final.last_event_id == "committed" and final.retry_milliseconds == 123
    assert final.discarded_pending_block and not final.discarded_unterminated_line
    assert parser._id_buffer == "committed"


@pytest.mark.parametrize(
    "value, expected",
    [("0", 0), ("000000", 0), ("00042", 42), (str(2**63 - 1), 2**63 - 1), ("0" * 9000 + "3", 3)],
)
def test_retry_conversion_is_bounded_before_int(value, expected):
    parser = SSEDecoder()
    parser.feed(f"retry: {value}\n".encode())
    assert parser.finish().snapshot.retry_milliseconds == expected


@pytest.mark.parametrize("value", [str(2**63), "9" * 9000, "0" * 9000 + str(2**63)])
def test_numeric_retry_overflow_is_explicit_bounded_profile(value):
    parser = SSEDecoder()
    with pytest.raises(SSEDecodeError, match="retry_limit"):
        parser.feed(f"retry: {value}\n".encode())


@pytest.mark.parametrize(
    "value", ["+1", "-1", "1.0", "1e2", "", " 1", "1 ", "\u0661", "\uff11\uff12", "0\0"]
)
def test_invalid_retry_is_ignored_even_if_large_nondecimal(value):
    parser = SSEDecoder()
    parser.feed(f"retry: 7\nretry: {value}\n\n".encode())
    assert parser.snapshot.retry_milliseconds == 7


@pytest.mark.parametrize("name", [f.name for f in dataclasses.fields(SSELimits)])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, "1", None, 1_000_000_000])
def test_limits_reject_nonexact_or_unbounded_configuration(name, value):
    with pytest.raises(ValueError):
        SSELimits(**{name: value})


@pytest.mark.parametrize(
    "settings, prefix, excess, code",
    [
        ({"max_input_bytes": 1}, b"x", b"x", "byte_limit"),
        ({"max_chunks": 1}, b"", b"", "chunk_limit"),
        ({"max_line_characters": 1}, b"x", b"x", "line_limit"),
        ({"max_data_characters": 2}, b"data:x\n", b"data:\n", "data_limit"),
        ({"max_data_fields": 1}, b"data:\n", b"data:\n", "data_field_limit"),
        ({"max_metadata_characters": 1}, b"id:x\n", b"id:xx\n", "metadata_limit"),
        ({"max_metadata_characters": 1}, b"event:x\n", b"event:xx\n", "metadata_limit"),
        ({"max_lines": 1}, b"\n", b"\n", "line_count_limit"),
        ({"max_events": 1}, b"data:\n\n", b"data:\n\n", "event_limit"),
        ({"max_output_characters": 7}, b"data:\n\n", b"data:\n\n", "output_limit"),
        ({"max_work": 1}, b"", b"", "work_limit"),
    ],
)
def test_every_limit_exact_boundary_and_one_beyond(settings, prefix, excess, code):
    parser = SSEDecoder(SSELimits(**settings))
    parser.feed(prefix)
    with pytest.raises(SSEDecodeError) as error:
        parser.feed(excess)
    assert error.value.code == code
    assert parser.snapshot.status is SSEStatus.FAILED
    assert not parser._data and not parser._line


def test_output_amplification_with_persistent_id_is_admitted_before_constructor(monkeypatch):
    parser = SSEDecoder(SSELimits(max_output_characters=1031))
    parser.feed(b"id:" + b"x" * 1024 + b"\ndata:\n\n")
    assert parser.snapshot.output_characters == 1031

    def forbidden(*args):
        raise AssertionError("over-budget event was materialized")

    monkeypatch.setattr(framing, "SSEEvent", forbidden)
    with pytest.raises(SSEDecodeError, match="output_limit"):
        parser.feed(b"data:\n\n")


def test_work_admission_precedes_constructor_and_exact_work_is_repeatable(monkeypatch):
    raw = b"data: value\n\n"
    reference = SSEDecoder()
    reference.feed(raw)
    cost = reference.snapshot.work
    assert SSEDecoder(SSELimits(max_work=cost)).feed(raw).events
    parser = SSEDecoder(SSELimits(max_work=cost - 1))
    monkeypatch.setattr(framing, "SSEEvent", lambda *a: pytest.fail("constructed before admission"))
    with pytest.raises(SSEDecodeError, match="work_limit"):
        parser.feed(raw)


def test_feed_delivery_atomicity_no_state_rollback_and_old_messages_remain_complete():
    parser = SSEDecoder(SSELimits(max_events=2))
    old = parser.feed(b"data: old\n\n")
    with pytest.raises(SSEDecodeError, match="event_limit"):
        parser.feed(b"data: internally-parsed\n\ndata: excess\n\n")
    assert event_rows(old.events) == [(0, "message", "old", "")]
    assert parser.snapshot.events == 2 and parser.snapshot.status is SSEStatus.FAILED


@pytest.mark.parametrize("chunk", ["data:x\n\n", bytearray(b"x"), memoryview(b"x"), None, 1])
def test_only_exact_bytes_are_admitted(chunk):
    parser = SSEDecoder()
    with pytest.raises(SSEDecodeError, match="chunk_type"):
        parser.feed(chunk)
    assert parser.snapshot.input_bytes == 0 and parser.snapshot.chunks == 0


def test_single_owner_reentry_and_control_failure_cleanup(monkeypatch):
    parser = SSEDecoder()
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        pytest.raises(RuntimeError, match="single-owner"),
    ):
        pool.submit(parser.feed, b"").result()
    assert parser.snapshot.status is SSEStatus.OPEN

    def nested(*args, **kwargs):
        parser.feed(b"")

    monkeypatch.setattr(parser, "_decode", nested)
    with pytest.raises(RuntimeError, match="not reentrant"):
        parser.feed(b"data:")
    assert parser.snapshot.status is SSEStatus.FAILED and not parser._busy
    second = SSEDecoder()
    second.feed(b"data: secret")

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt("user interruption")

    monkeypatch.setattr(second, "_decode", interrupted)
    with pytest.raises(KeyboardInterrupt, match="user interruption"):
        second.finish()
    assert second.snapshot.status is SSEStatus.FAILED and second._line == []


def test_terminal_lifecycle_close_is_idempotent_no_eof_inference():
    parser = SSEDecoder()
    parser.feed(b"id: committed\n\ndata: pending")
    assert parser.feed(b"").snapshot.status is SSEStatus.OPEN
    parser.close()
    parser.close()
    assert parser.snapshot.status is SSEStatus.CLOSED
    assert parser.snapshot.last_event_id == "committed"
    assert parser._line == []
    for operation in (lambda: parser.feed(b""), parser.finish):
        with pytest.raises(RuntimeError, match="terminal"):
            operation()
    finished = SSEDecoder()
    finished.finish()
    finished.close()
    assert finished.snapshot.status is SSEStatus.FINISHED


@pytest.mark.parametrize(
    "kwargs", [{"limits": {}}, {"utf8_errors": "ignore"}, {"utf8_errors": None}]
)
def test_decoder_configuration(kwargs):
    with pytest.raises(ValueError):
        SSEDecoder(**kwargs)


@pytest.mark.parametrize(
    "changes",
    [
        {"sequence": True},
        {"sequence": -1},
        {"sequence": 100_000},
        {"data": "\ud800"},
        {"data": 1},
        {"data": "x" * 8_000_001},
        {"event_type": ""},
        {"event_type": "\n"},
        {"event_type": "\ud800"},
        {"last_event_id": "\0"},
        {"last_event_id": "\r"},
        {"last_event_id": "x" * 16_385},
    ],
)
def test_immutable_event_public_construction_is_validated(changes):
    values = {"sequence": 0, "event_type": "message", "data": "secret", "last_event_id": "private"}
    with pytest.raises(ValueError):
        SSEEvent(**(values | changes))


def test_event_immutability_and_safe_repr():
    event = SSEEvent(0, "sensitive-type", "secret", "private-id")
    assert "secret" not in repr(event) and "private-id" not in repr(event)
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.data = "changed"
    parser = SSEDecoder()
    parser.feed(b"id: secret\n\n")
    assert "secret" not in repr(parser.snapshot)


def test_executable_offline_application_protocol_example():
    example = runpy.run_path(
        str(Path(__file__).resolve().parents[1] / "examples/sse_json_output.py")
    )
    assert example["run_example"]() == {
        "valid": True,
        "messages": 3,
        "output": {"label": "雪🙂", "score": 2},
    }
