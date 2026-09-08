from __future__ import annotations

import json
import math
import random
import runpy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from itertools import pairwise
from pathlib import Path

import pytest

from payload_palette.errors import PayloadValidationError
from payload_palette.incremental_output import (
    CompletedJSONValue,
    IncrementalJSONError,
    IncrementalLimits,
    IncrementalOutputSession,
    IncrementalProgress,
    IncrementalResult,
    IncrementalStatistics,
    IncrementalStatus,
    JSONValueSnapshot,
    validate_output_chunks,
)
from payload_palette.ingress import decode_json_bytes
from payload_palette.output_schema import OutputContractError, OutputLimits, OutputSchema
from payload_palette.output_validation import (
    RuleBinding,
    RuleResult,
    TrimmedString,
    ValidationPipeline,
)


def pipeline_for(value):
    if type(value) is dict:
        schema = OutputSchema("object", additional_properties=True)
    elif type(value) is list:
        # The parser's differential check must still agree when final schema
        # rejection is correct. This deliberately disallows arrays as items.
        schema = OutputSchema(
            "array",
            items=OutputSchema(
                "union",
                any_of=(
                    OutputSchema("null"),
                    OutputSchema("boolean"),
                    OutputSchema("integer"),
                    OutputSchema("number"),
                    OutputSchema("string"),
                    OutputSchema("object", additional_properties=True),
                ),
            ),
        )
    else:
        schema = OutputSchema(
            {type(None): "null", bool: "boolean", int: "integer", float: "number", str: "string"}[
                type(value)
            ]
        )
    return ValidationPipeline(schema)


def stdlib_strict(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate")
            result[key] = value
        return result

    def integer(text):
        if len(text.removeprefix("-")) > 256:
            raise ValueError("integer range")
        return int(text)

    def floating(text):
        result = float(text)
        if not math.isfinite(result):
            raise ValueError("float range")
        return result

    def constant(text):
        raise ValueError("nonstandard")

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique,
        parse_int=integer,
        parse_float=floating,
        parse_constant=constant,
    )

    def unicode_tree(value):
        if type(value) is str and any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ValueError("surrogate")
        if type(value) is list:
            for child in value:
                unicode_tree(child)
        if type(value) is dict:
            for key, child in value.items():
                unicode_tree(key)
                unicode_tree(child)

    unicode_tree(value)
    return value


VALID = [
    b"null",
    b"true",
    b"false",
    b"0",
    b"-0",
    b"42",
    b"-123",
    b"1.0",
    b"-0.0",
    b"0.125",
    b"1E+12",
    b"1e-9999",
    b"-1e-9999",
    b"1.7976931348623157e308",
    b'""',
    b'"\\"\\\\\\/\\b\\f\\n\\r\\t"',
    b'"\\u0000\\u007f\\u0800"',
    b'"\\ud83d\\ude00"',
    b'"\\uDBFF\\uDFFF"',
    '"中文😀é"'.encode(),
    b"[]",
    b"{}",
    b' {"a":1,"b":[true,null,{"z":"x"}]} \t\r\n',
    b'{"\\ud83d\\ude00":{"a.b":[1,2,3]}}',
    b'{"":null,"x":"comma, bracket] brace}"}',
    b'[0,1.0,-0.0,{"nested":[[],{}]}]',
    b'"ordinary surrogate-looking text d800"',
]


@pytest.mark.parametrize("raw", VALID)
def test_every_byte_split_matches_independent_stdlib_existing_strict_and_whole_pipeline(raw):
    reference = stdlib_strict(raw)
    assert decode_json_bytes(raw) == reference
    pipeline = pipeline_for(reference)
    expected = pipeline.validate_json_bytes(raw)
    for split in range(len(raw) + 1):
        session = IncrementalOutputSession(pipeline)
        events = [*session.feed(raw[:split]).events, *session.feed(raw[split:]).events]
        final = session.finish()
        events.extend(final.final_events)
        assert final.report == expected, (raw, split)
        assert events[-1].path == ()
        assert events[-1].value.to_python() == reference
        assert events[-1].index + 1 == final.statistics.nodes_started
        assert final.statistics.status is IncrementalStatus.FINISHED
        assert final.statistics.characters_consumed == len(raw.decode())
    one_byte = validate_output_chunks((bytes([byte]) for byte in raw), pipeline)
    assert one_byte.report == expected


def test_seeded_nested_corpus_has_chunk_independent_exact_whole_reports():
    rng = random.Random(271828)

    def value(depth):
        if depth <= 0 or rng.randrange(3) == 0:
            return rng.choice(
                [None, False, True, rng.randrange(-1000, 1000), rng.uniform(-2, 2), "😀\n值", ""]
            )
        if rng.randrange(2):
            return [value(depth - 1) for _ in range(rng.randrange(5))]
        return {f"key-{index}-é": value(depth - 1) for index in range(rng.randrange(5))}

    for _ in range(150):
        document = value(5)
        raw = json.dumps(
            document, ensure_ascii=rng.choice([True, False]), separators=(",", ":")
        ).encode()
        pipeline = pipeline_for(document)
        cuts = sorted({0, len(raw), *(rng.randrange(len(raw) + 1) for _ in range(12))})
        chunks = [raw[a:b] for a, b in pairwise(cuts)]
        result = validate_output_chunks(chunks, pipeline)
        assert result.report == pipeline.validate_json_bytes(raw)
        assert stdlib_strict(raw) == document


INVALID = [
    b"",
    b" ",
    b"+1",
    b"01",
    b"-01",
    b"--1",
    b"1.",
    b".1",
    b"1e",
    b"1e+",
    b"1e-",
    b"1e++1",
    b"1..0",
    b"1.e2",
    b"1E2.3",
    b"1e9999",
    b"-1e9999",
    b"9" * 257,
    b"NaN",
    b"Infinity",
    b"-Infinity",
    b"TRUE",
    b"tru",
    b"trze",
    b"falsx",
    b"nulx",
    b"{}{}",
    b"1 2",
    b"truefalse",
    b"[]null",
    b"[1,]",
    b'{"a":1,}',
    b"[,1]",
    b"[1 2]",
    b"{a:1}",
    b'{"a" 1}',
    b'{"a":}',
    b"[}",
    b"{]",
    b"[",
    b"{",
    b'"unterminated',
    b'"\\x"',
    b'"\\u+123"',
    b'"\\u12xz"',
    b'"\\u123"',
    b'"\\ud800"',
    b'"\\udc00"',
    b'"\\ud800\\u0000"',
    b'"\\ud800x"',
    b'"\\ud800\\n"',
    b'"\n"',
    b'"\x00"',
    b"\xef\xbb\xbf{}",
    b'"\xed\xa0\x80"',
    b'"\xc0\x80"',
    b'"\xf4\x90\x80\x80"',
    b'{"x":1,"\\u0078":2}',
    '{"😀":1,"\\ud83d\\ude00":2}'.encode(),
    b"{}\v",
    b"[]\f",
    b"1\xc2\xa0",
    b"null # comment",
    b"/*x*/0",
    b'"\xe4\xb8',
]


@pytest.mark.parametrize("raw", INVALID)
def test_invalid_documents_are_never_accepted_at_any_byte_split(raw):
    with pytest.raises((ValueError, UnicodeError)):
        stdlib_strict(raw)
    with pytest.raises(PayloadValidationError):
        decode_json_bytes(raw)
    for split in range(len(raw) + 1):
        session = IncrementalOutputSession(
            ValidationPipeline(OutputSchema("object", additional_properties=True))
        )
        with pytest.raises(IncrementalJSONError):
            session.feed(raw[:split])
            session.feed(raw[split:])
            session.finish()
        assert session.statistics.status is IncrementalStatus.FAILED
        with pytest.raises(RuntimeError, match="terminal"):
            session.feed(b"{}")
        with pytest.raises(RuntimeError, match="terminal"):
            session.finish()


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"-",
        b"1.",
        b"1e",
        b"1e+",
        b'"x',
        b'"\\',
        b'"\\u12',
        b'"\\ud800',
        b'"\\ud800\\uDC',
        b"tru",
        b"[1,",
        b'{"a":1',
    ],
)
def test_incomplete_but_extendable_prefix_is_distinct_from_invalid_syntax(raw):
    session = IncrementalOutputSession(pipeline_for({}))
    session.feed(raw)
    with pytest.raises(IncrementalJSONError) as caught:
        session.finish()
    assert caught.value.code == "incomplete_json"


def test_incomplete_utf8_and_invalid_utf8_have_distinct_terminal_errors():
    session = IncrementalOutputSession(pipeline_for(""))
    session.feed(b'"\xf0\x9f')
    with pytest.raises(IncrementalJSONError) as incomplete:
        session.finish()
    assert incomplete.value.code == "incomplete_utf8"
    session = IncrementalOutputSession(pipeline_for(""))
    with pytest.raises(IncrementalJSONError) as invalid:
        session.feed(b'"\xff')
    assert invalid.value.code == "invalid_utf8"


def test_progress_precedes_document_end_is_immutable_and_shares_completed_subtrees(monkeypatch):
    session = IncrementalOutputSession(pipeline_for({}))
    with monkeypatch.context() as patch:
        patch.setattr(json, "loads", lambda *args, **kwargs: pytest.fail("prefix reparsing"))
        first = session.feed(b'{"items":[1,')
        second = session.feed(b'{"ok":true}],"name":" x "}')
    assert len(first.events) == 1 and first.events[0].path == ("items", 0)
    assert first.events[0].value.scalar == 1
    root = second.events[-1].value
    array = root.properties[0][1]
    assert array.items[0] is first.events[0].value
    assert array.items[1].properties[0][1] is second.events[0].value
    assert second.statistics.root_complete and second.statistics.status is IncrementalStatus.OPEN
    assert not hasattr(first, "valid") and not hasattr(first.events[0], "accepted")
    with pytest.raises(FrozenInstanceError):
        first.events[0].index = 99
    detached = root.to_python()
    detached["items"][0] = 99
    assert root.to_python()["items"][0] == 1
    assert session.finish().report.output == root.to_python()


def test_number_completion_waits_for_delimiter_or_eof_without_premature_event():
    session = IncrementalOutputSession(pipeline_for(1.0))
    assert not session.feed(b"1").events
    assert not session.feed(b".5e").events
    assert not session.feed(b"+2").events
    final = session.finish()
    assert len(final.final_events) == 1
    assert final.final_events[0].value.scalar == 150.0
    assert final.final_events[0].end_character == 6
    assert final.report.output == 150.0
    with pytest.raises(RuntimeError):
        session.feed(b"0")
    with pytest.raises(RuntimeError):
        session.finish()


def test_number_delimiter_event_offset_excludes_unconsumed_delimiter():
    session = IncrementalOutputSession(pipeline_for([1]))
    progress = session.feed(b"[12,3] ")
    assert [event.end_character for event in progress.events] == [3, 5, 6]
    assert session.finish().statistics.characters_consumed == 7


def test_decoded_key_uniqueness_fails_before_second_value_and_never_runs_rules():
    calls = []

    class Count:
        def check(self, value, context):
            calls.append(value)
            return RuleResult(True)

    pipeline = ValidationPipeline(
        OutputSchema("object", properties={"x": OutputSchema("integer")}),
        (RuleBinding("count", ("x",), Count()),),
    )
    session = IncrementalOutputSession(pipeline)
    provisional = session.feed(b'{"x":1,')
    assert provisional.events[0].value.scalar == 1 and not calls
    with pytest.raises(IncrementalJSONError) as error:
        session.feed(b'"\\u0078"')
    assert error.value.code == "duplicate_json_key" and not calls
    assert (
        provisional.statistics.status is IncrementalStatus.OPEN
    )  # Snapshot, not live session state.
    assert session.statistics.status is IncrementalStatus.FAILED


def test_semantic_fix_runs_only_once_after_actual_finish_and_retains_raw_provisional_value():
    calls = []

    class CountTrim:
        def check(self, value, context):
            calls.append((context.phase, value))
            return TrimmedString().check(value, context)

    schema = OutputSchema("object", properties={"name": OutputSchema("string")}, required=("name",))
    pipeline = ValidationPipeline(schema, (RuleBinding("trim", ("name",), CountTrim(), "fix"),))
    session = IncrementalOutputSession(pipeline)
    assert not session.feed(b'{"name":"  ').events and not calls
    progress = session.feed(b'alice "}')
    assert progress.events[0].value.scalar == "  alice " and not calls
    session.feed(b" \n")
    result = session.finish()
    assert calls == [("initial", "  alice "), ("repair", "alice"), ("final", "alice")]
    assert result.report.output == {"name": "alice"}
    assert progress.events[0].value.scalar == "  alice "


@pytest.mark.parametrize("schema", [OutputSchema("integer"), OutputSchema("string", min_length=5)])
def test_complete_syntax_schema_rejection_is_finished_not_parse_failed(schema):
    result = validate_output_chunks([b'"x"'], ValidationPipeline(schema))
    assert not result.report.valid
    assert result.statistics.status is IncrementalStatus.FINISHED
    assert result.report == ValidationPipeline(schema).validate_json_bytes(b'"x"')


def test_pipeline_exception_fails_and_clears_session(monkeypatch):
    session = IncrementalOutputSession(pipeline_for(1))
    session.feed(b"1 ")
    error = KeyboardInterrupt()

    def failed(*args):
        raise error

    monkeypatch.setattr(ValidationPipeline, "validate", failed)
    with pytest.raises(KeyboardInterrupt) as caught:
        session.finish()
    assert caught.value is error and session.statistics.status is IncrementalStatus.FAILED
    assert not session._parts and not session._stack and session._root is None


@pytest.mark.parametrize("raw", [b"9" * 256, b"-" + b"9" * 256])
def test_strict_ingress_integer_and_output_numeric_ranges_remain_distinct(raw):
    value = decode_json_bytes(raw)
    pipeline = pipeline_for(1)
    with pytest.raises(OutputContractError):
        pipeline.validate(value)
    session = IncrementalOutputSession(pipeline)
    session.feed(raw)
    with pytest.raises(OutputContractError):
        session.finish()
    assert session.statistics.status is IncrementalStatus.FAILED


@pytest.mark.parametrize(
    "option, raw, code",
    [
        ({"max_input_bytes": 1}, b"12", "byte_limit"),
        ({"max_token_characters": 2}, b'"ab"', "token_character_limit"),
        ({"max_token_characters": 2}, b"123", "token_character_limit"),
        ({"max_token_characters": 2}, b"true", "token_character_limit"),
        ({"max_tokens": 1}, b"[]", "token_limit"),
        ({"max_work": 1}, b"[]", "work_limit"),
    ],
)
def test_parser_limits_stop_before_over_budget_allocation_or_acceptance(option, raw, code):
    session = IncrementalOutputSession(pipeline_for({}), IncrementalLimits(**option))
    with pytest.raises(IncrementalJSONError) as caught:
        session.feed(raw)
        session.finish()
    assert caught.value.code == code
    assert session.statistics.status is IncrementalStatus.FAILED


def test_empty_chunks_count_towards_work_and_chunk_limit_are_not_eof():
    session = IncrementalOutputSession(pipeline_for(1), IncrementalLimits(max_chunks=2))
    assert not session.feed(b"").events
    assert not session.feed(b"").statistics.root_complete
    with pytest.raises(IncrementalJSONError, match="chunk_limit"):
        session.feed(b"1")
    assert session.statistics.chunks_received == 2


@pytest.mark.parametrize(
    "limits, raw, code",
    [
        (OutputLimits(max_depth=1), b"[[0]]", "depth_limit"),
        (OutputLimits(max_nodes=2), b"[0,1]", "node_limit"),
        (OutputLimits(max_characters=1), b'{"a":"b"}', "character_limit"),
    ],
)
def test_output_graph_limits_match_existing_snapshot_accounting(limits, raw, code):
    pipeline = ValidationPipeline(pipeline_for(json.loads(raw)).schema, limits=limits)
    with pytest.raises(OutputContractError):
        pipeline.validate_json_bytes(raw)
    with pytest.raises(IncrementalJSONError, match=code):
        validate_output_chunks([raw], pipeline)


@pytest.mark.parametrize(
    "raw, limits",
    [
        (b"[[]]", OutputLimits(max_depth=1, max_nodes=2)),
        (b'{"a":"\\ud83d\\ude00"}', OutputLimits(max_characters=2, max_nodes=2, max_depth=1)),
        (b'""', OutputLimits(max_nodes=1, max_depth=1, max_characters=1)),
    ],
)
def test_exact_output_budget_boundaries_are_inclusive(raw, limits):
    pipeline = ValidationPipeline(pipeline_for(json.loads(raw)).schema, limits=limits)
    assert validate_output_chunks([raw], pipeline).report == pipeline.validate_json_bytes(raw)


def test_nested_empty_container_depth_matches_root_zero_depth_not_container_count():
    raw = b"[" * 65 + b"]" * 65
    pipeline = ValidationPipeline(
        OutputSchema("array", items=OutputSchema("null")), limits=OutputLimits(max_depth=64)
    )
    assert validate_output_chunks([raw], pipeline).report == pipeline.validate_json_bytes(raw)
    with pytest.raises(IncrementalJSONError, match="depth_limit"):
        validate_output_chunks([b"[" + raw + b"]"], pipeline)


def test_event_path_amplification_is_charged_before_event_construction():
    key = b"k" * 100
    session = IncrementalOutputSession(pipeline_for({}), IncrementalLimits(max_work=500))
    session.feed(b'{"' + key + b'":[')
    with pytest.raises(IncrementalJSONError, match="work_limit"):
        session.feed(b"0," * 10)
    assert session.statistics.work <= 500 and session.statistics.values_completed < 10


@pytest.mark.parametrize("count", [100, 200, 400])
def test_one_byte_chunks_have_exact_linear_character_and_event_work(count):
    raw = b"[" + b",".join(b"0" for _ in range(count)) + b"]"
    result = validate_output_chunks((bytes([byte]) for byte in raw), pipeline_for([]))
    assert result.statistics.nodes_started == count + 1
    assert result.statistics.values_completed == count + 1
    assert result.statistics.work == len(raw) + 4 * count + len(raw) + 2
    assert result.report.output == [0] * count


@pytest.mark.parametrize("bad", ["{}", bytearray(b"{}"), memoryview(b"{}"), None, True])
def test_feed_requires_exact_owned_bytes_and_bad_chunk_is_terminal(bad):
    session = IncrementalOutputSession(pipeline_for({}))
    with pytest.raises(IncrementalJSONError, match="chunk_type"):
        session.feed(bad)
    assert session.statistics.status is IncrementalStatus.FAILED


def test_close_is_idempotent_and_no_validation_occurs_for_abandoned_prefix():
    session = IncrementalOutputSession(pipeline_for({}))
    session.feed(b'{"x":[')
    session.close()
    session.close()
    assert session.statistics.status is IncrementalStatus.CLOSED
    assert not session._stack and not session._parts
    with pytest.raises(RuntimeError):
        session.finish()


def test_cross_thread_and_reentrant_calls_cannot_modify_session():
    pipeline = pipeline_for(1)
    session = IncrementalOutputSession(pipeline)
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        pytest.raises(RuntimeError, match="single-owner"),
    ):
        pool.submit(session.feed, b"1").result()
    assert session.statistics.nodes_started == 0

    class Probe:
        def check(self, value, context):
            with pytest.raises(RuntimeError, match="reentrant"):
                guarded.close()
            return RuleResult(True)

    guarded = IncrementalOutputSession(
        ValidationPipeline(OutputSchema("integer"), (RuleBinding("probe", (), Probe()),))
    )
    guarded.feed(b"1")
    assert guarded.finish().report.valid


class SourceIterator:
    def __init__(self, chunks, *, error=None, close_error=None):
        self.chunks = iter(chunks)
        self.error = error
        self.close_error = close_error
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.error is not None:
                raise self.error from None
            raise

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


@pytest.mark.parametrize("owned", [False, True])
def test_pull_source_ownership_is_explicit_and_cleanup_precedes_semantic_validation(owned):
    source = SourceIterator([b'"x"'])
    observed = []

    class Probe:
        def check(self, value, context):
            assert source.closed is owned
            return RuleResult(True)

    pipeline = ValidationPipeline(OutputSchema("string"), (RuleBinding("probe", (), Probe()),))
    result = validate_output_chunks(
        source, pipeline, on_progress=observed.append, close_iterator=owned
    )
    assert result.report.valid and len(observed) == 1
    assert observed[0].statistics.status is IncrementalStatus.OPEN
    assert source.closed is owned


@pytest.mark.parametrize(
    "primary", [RuntimeError("source failed"), KeyboardInterrupt(), SystemExit(7)]
)
@pytest.mark.parametrize("during", ["iteration", "observer"])
def test_pull_combined_primary_and_close_failures_preserve_original_exception(primary, during):
    source = SourceIterator(
        [b"1"],
        error=primary if during == "iteration" else None,
        close_error=OSError("close failed"),
    )

    def observer(progress):
        if during == "observer":
            raise primary

    with pytest.raises(type(primary)) as caught:
        validate_output_chunks(source, pipeline_for(1), on_progress=observer, close_iterator=True)
    assert caught.value is primary and source.closed
    assert "incremental output iterator cleanup also failed" in primary.__notes__


@pytest.mark.parametrize(
    "primary", [None, RuntimeError("source failed"), KeyboardInterrupt(), SystemExit(7)]
)
def test_pull_cleanup_lookup_failure_also_preserves_primary(primary):
    class Source(SourceIterator):
        @property
        def close(self):
            raise OSError("close lookup failed")

    source = Source([b"1"], error=primary)
    with pytest.raises(OSError if primary is None else type(primary)) as caught:
        validate_output_chunks(source, pipeline_for(1), close_iterator=True)
    if primary is not None:
        assert caught.value is primary and primary.__notes__


@pytest.mark.parametrize(
    "primary", [None, RuntimeError("source failed"), KeyboardInterrupt(), SystemExit(7)]
)
@pytest.mark.parametrize("kind", ["async_method", "awaitable", "suspended", "suspended_error"])
def test_async_iterator_cleanup_is_rejected_and_cannot_approve(primary, kind):
    calls, cleaned, coroutines = [], [], []

    class Suspend:
        def __await__(self):
            yield

    async def suspended():
        try:
            await Suspend()
        finally:
            cleaned.append(True)
            if kind == "suspended_error":
                raise RuntimeError("cleanup detail")

    class Source(SourceIterator):
        def close(self):
            if kind == "awaitable":
                return Suspend()
            coroutine = suspended()
            coroutines.append(coroutine)
            if kind != "async_method":
                coroutine.send(None)
            return coroutine

    class Count:
        def check(self, value, context):
            calls.append(value)
            return RuleResult(True)

    class AsyncSource(SourceIterator):
        async def close(self):
            cleaned.append(True)

    source = (AsyncSource if kind == "async_method" else Source)([b"1"], error=primary)
    pipeline = ValidationPipeline(OutputSchema("integer"), (RuleBinding("count", (), Count()),))
    with pytest.raises(ValueError if primary is None else type(primary)) as caught:
        validate_output_chunks(source, pipeline, close_iterator=True)
    if primary is not None:
        assert caught.value is primary and primary.__notes__
    else:
        assert "iterator.close must be synchronous" in str(caught.value)
    assert not calls
    assert cleaned == ([True] if kind.startswith("suspended") else [])
    assert all(coroutine.cr_frame is None for coroutine in coroutines)


@pytest.mark.parametrize("primary", [None, RuntimeError("source failed"), KeyboardInterrupt()])
def test_returned_cleanup_coroutine_genuine_interrupt_propagates(primary):
    class Suspend:
        def __await__(self):
            yield

    async def suspended():
        try:
            await Suspend()
        finally:
            raise SystemExit(17)

    class Source(SourceIterator):
        def close(self):
            coroutine = suspended()
            coroutine.send(None)
            return coroutine

    with pytest.raises(SystemExit, match="17"):
        validate_output_chunks(Source([b"1"], error=primary), pipeline_for(1), close_iterator=True)


@pytest.mark.parametrize("primary", [None, RuntimeError("source failed"), KeyboardInterrupt()])
def test_async_generator_close_method_cannot_silently_approve(primary):
    calls = []

    class Source(SourceIterator):
        async def close(self):
            calls.append("async cleanup ran")
            yield

    class Count:
        def check(self, value, context):
            calls.append("validation ran")
            return RuleResult(True)

    pipeline = ValidationPipeline(OutputSchema("integer"), (RuleBinding("count", (), Count()),))
    with pytest.raises(ValueError if primary is None else type(primary)) as caught:
        validate_output_chunks(Source([b"1"], error=primary), pipeline, close_iterator=True)
    if primary is not None:
        assert caught.value is primary and primary.__notes__
    assert not calls


@pytest.mark.parametrize("error", [OSError("close failed"), KeyboardInterrupt(), SystemExit(3)])
def test_pull_cleanup_failure_blocks_approval_before_semantic_invocation(error):
    calls = []

    class Count:
        def check(self, value, context):
            calls.append(value)
            return RuleResult(True)

    pipeline = ValidationPipeline(OutputSchema("integer"), (RuleBinding("count", (), Count()),))
    source = SourceIterator([b"1"], close_error=error)
    with pytest.raises(type(error)) as caught:
        validate_output_chunks(source, pipeline, close_iterator=True)
    assert caught.value is error and source.closed and not calls


def test_cleanup_interrupt_is_not_hidden_by_an_ordinary_source_error():
    source = SourceIterator([], error=RuntimeError("primary"), close_error=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        validate_output_chunks(source, pipeline_for(1), close_iterator=True)
    assert source.closed


def test_owned_iterator_with_no_close_method_remains_usable():
    assert validate_output_chunks([b"1"], pipeline_for(1), close_iterator=True).report.output == 1


def test_invalid_stream_syntax_closes_owned_source_and_does_not_consume_more_chunks():
    source = SourceIterator([b"bad", b"{}"])
    with pytest.raises(IncrementalJSONError):
        validate_output_chunks(source, pipeline_for({}), close_iterator=True)
    assert source.closed and next(source.chunks) == b"{}"


@pytest.mark.parametrize("value", [True, 0, "text", object()])
def test_progress_observer_must_return_none_and_failure_closes_source(value):
    source = SourceIterator([b"1"])
    with pytest.raises(ValueError, match="return None"):
        validate_output_chunks(
            source, pipeline_for(1), on_progress=lambda progress: value, close_iterator=True
        )
    assert source.closed


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_progress_observer_returned_coroutine_is_closed_and_rejected(cleanup_error):
    closed = []

    class Suspend:
        def __await__(self):
            yield

    async def invalid():
        try:
            await Suspend()
        finally:
            closed.append(True)
            if cleanup_error:
                raise RuntimeError("secondary cleanup detail")

    def callback(progress):
        coroutine = invalid()
        coroutine.send(None)
        return coroutine

    source = SourceIterator([b"1"])
    with pytest.raises(ValueError, match="return None"):
        validate_output_chunks(source, pipeline_for(1), on_progress=callback, close_iterator=True)
    assert closed == [True] and source.closed


def test_progress_callback_and_ownership_preflight_happen_before_iterating():
    class Source:
        def __iter__(self):
            pytest.fail("source consumed before callback admission")

    async def asynchronous(progress):
        pass

    class AsyncCallable:
        async def __call__(self, progress):
            pass

    for callback in (False, 1, asynchronous, AsyncCallable()):
        with pytest.raises(ValueError, match="synchronous"):
            validate_output_chunks(Source(), pipeline_for(1), on_progress=callback)
    with pytest.raises(ValueError, match="boolean"):
        validate_output_chunks(Source(), pipeline_for(1), close_iterator=1)
    with pytest.raises(TypeError):
        validate_output_chunks(None, pipeline_for(1))


@pytest.mark.parametrize("field", list(IncrementalLimits.__dataclass_fields__))
def test_additional_limit_fields_reject_boolean_zero_huge_and_mutable_values(field):
    for value in (True, 0, -1, 1.5, 10**1000, []):
        with pytest.raises(ValueError):
            IncrementalLimits(**{field: value})


def test_session_config_references_are_read_only():
    with pytest.raises(ValueError, match="pipeline"):
        IncrementalOutputSession({})
    with pytest.raises(ValueError, match="limits"):
        IncrementalOutputSession(pipeline_for(1), {})
    session = IncrementalOutputSession(pipeline_for(1))
    with pytest.raises(AttributeError):
        session.pipeline = pipeline_for("")
    with pytest.raises(AttributeError):
        session.limits = IncrementalLimits(max_chunks=1)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"kind": []},
        {"kind": "wrong"},
        {"kind": "array", "items": []},
        {"kind": "object", "properties": []},
        {"kind": "array", "items": (None,)},
        {"kind": "object", "properties": ((1, None),)},
        {"kind": "object", "properties": (("a",),)},
        {"kind": "object", "properties": (("a", None),)},
        {"kind": "string", "scalar": "\ud800"},
        {"kind": "integer", "scalar": True},
        {"kind": "integer", "scalar": 10**256},
        {"kind": "number", "scalar": float("inf")},
        {"kind": "number", "scalar": 1},
        {"kind": "null", "scalar": False},
        {"kind": "boolean", "scalar": 1},
        {"kind": "array", "scalar": 0},
    ],
)
def test_snapshot_public_constructor_requires_strict_immutable_scalar_tree(kwargs):
    with pytest.raises(ValueError):
        JSONValueSnapshot(**kwargs)


def test_snapshot_constructor_bounds_cached_tree_totals_without_rewalking_subtrees():
    null = JSONValueSnapshot("null")
    with pytest.raises(ValueError, match="children disagree"):
        JSONValueSnapshot("string", "x", items=(null,))
    with pytest.raises(ValueError, match="children disagree"):
        JSONValueSnapshot("array", properties=(("x", null),))
    with pytest.raises(ValueError, match="unique"):
        JSONValueSnapshot("object", properties=(("x", null), ("x", null)))
    with pytest.raises(ValueError, match="child count"):
        JSONValueSnapshot("array", items=(null,) * 100_001)
    with pytest.raises(ValueError, match="graph ceilings"):
        JSONValueSnapshot("array", items=(null,) * 100_000)
    root = null
    for _ in range(64):
        root = JSONValueSnapshot("array", items=(root,))
    with pytest.raises(ValueError, match="graph ceilings"):
        JSONValueSnapshot("array", items=(root,))
    with pytest.raises(ValueError, match="graph ceilings"):
        JSONValueSnapshot("string", "a" * 8_000_001)


def test_snapshot_leaf_range_includes_strict_json_256_digit_maximum():
    assert JSONValueSnapshot("integer", 10**256 - 1).to_python() == 10**256 - 1
    assert JSONValueSnapshot("number", -0.0).to_python() == 0.0


@pytest.fixture
def progress():
    return IncrementalOutputSession(pipeline_for([0, 1])).feed(b"[0,1]")


@pytest.mark.parametrize(
    "changes",
    [
        {"index": True},
        {"index": -1},
        {"index": 100_000},
        {"end_character": -1},
        {"path": []},
        {"path": (None,)},
        {"path": (True,)},
        {"path": (-1,)},
        {"path": ("x",) * 65},
        {"path": ("\ud800",)},
        {"value": None},
    ],
)
def test_completed_event_public_fields_are_strict(progress, changes):
    with pytest.raises(ValueError):
        replace(progress.events[0], **changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "open"},
        {"root_complete": 1},
        {"bytes_received": True},
        {"work": 10**1000},
        {"values_completed": 4},
        {"characters_consumed": 6},
        {"root_complete": True, "nodes_started": 4},
        {"status": IncrementalStatus.FINISHED, "root_complete": False},
    ],
)
def test_progress_statistics_reject_impossible_counter_records(progress, changes):
    with pytest.raises(ValueError):
        replace(progress.statistics, **changes)


def test_progress_and_final_batch_constructors_validate_order_and_status(progress):
    with pytest.raises(ValueError):
        IncrementalProgress(progress.events, None)
    with pytest.raises(ValueError):
        IncrementalProgress([], progress.statistics)
    with pytest.raises(ValueError):
        IncrementalProgress((None,), progress.statistics)
    with pytest.raises(ValueError):
        IncrementalProgress(progress.events * 2, progress.statistics)
    with pytest.raises(ValueError):
        IncrementalProgress((replace(progress.events[0], index=3),), progress.statistics)
    with pytest.raises(ValueError):
        IncrementalProgress((replace(progress.events[0], end_character=100),), progress.statistics)
    with pytest.raises(ValueError):
        IncrementalProgress((progress.events[0], progress.events[2]), progress.statistics)
    with pytest.raises(ValueError):
        IncrementalProgress(
            (progress.events[0], replace(progress.events[1], end_character=0)), progress.statistics
        )
    result = validate_output_chunks([b"1"], pipeline_for(1))
    with pytest.raises(ValueError):
        replace(result, report=None)
    with pytest.raises(ValueError):
        replace(result, statistics=progress.statistics)
    assert isinstance(result, IncrementalResult) and isinstance(progress, IncrementalProgress)
    assert isinstance(result.final_events[0], CompletedJSONValue)
    assert isinstance(result.statistics, IncrementalStatistics)


def test_offline_model_chunk_example_reports_provisional_progress_and_complete_validation(capsys):
    runpy.run_path(
        str(Path(__file__).parents[1] / "examples" / "incremental_model_output.py"),
        run_name="__main__",
    )
    data = json.loads(capsys.readouterr().out)
    assert data["source_closed"] is True
    assert data["syntax_events_before_finish"] > 0
    assert data["report"]["valid"] is True
    assert data["report"]["output"] == {"label": "cat", "caption": "猫 😀"}


def test_incremental_api_exports_are_public():
    import payload_palette

    for name in (
        "CompletedJSONValue",
        "IncrementalOutputSession",
        "IncrementalProgress",
        "IncrementalResult",
        "IncrementalStatus",
        "JSONValueSnapshot",
        "validate_output_chunks",
    ):
        assert getattr(payload_palette, name) is globals()[name]
