# Bounded offline event-stream framing

`SSEDecoder` incrementally consumes exact byte chunks and returns immutable complete
`SSEEvent` objects. It does not open HTTP connections, call model APIs, reconnect,
parse JSON, recognize provider finish reasons or approve model output. No API key
or network access is required. This is a finite, resource-bounded framing profile,
not the browser `EventSource` API.

```python
from payload_palette import SSEDecoder

decoder = SSEDecoder()
first = decoder.feed(b"id: 7\nevent: update\ndata: hel")
second = decoder.feed(b"lo\n\n")
assert first.events == ()
assert second.events[0].data == "hello"
assert second.events[0].last_event_id == "7"
final = decoder.finish()
assert final.events == ()
```

## Framing contract

The implementation follows the line and field interpretation in the
[WHATWG event-stream specification](https://html.spec.whatwg.org/multipage/server-sent-events.html#event-stream-interpretation):
CRLF, CR and LF terminate lines; the first colon separates a field from its value,
with at most one leading ASCII space removed. Field names are case-sensitive.
Comments and unknown fields are ignored. Data fields join with LF and only a blank
line dispatches a message. An empty data field produces an empty message; absence
of data does not. Message type defaults to `message` and resets at every blank block.
ID persists, is committed at a blank block even without data, and ignores NUL values.
EOF discards unterminated lines and pending blocks instead of synthesizing a message.

The [Encoding Standard's UTF-8 decode algorithm](https://encoding.spec.whatwg.org/#utf-8-decode)
uses replacement decoding and strips one leading BOM. The default follows that
behavior, including truncated UTF-8 at EOF. A plain incremental UTF-8 decoder plus
a first-codepoint BOM gate avoids partial-BOM loss in some Python `utf-8-sig`
implementations. `utf8_errors="strict"` explicitly selects a narrower profile:
malformed UTF-8 fails instead of being replaced. Replacement can alter application
text; callers requiring byte-faithful valid UTF-8 should choose strict decoding.

Valid `retry` fields update an integer millisecond value as soon as their line is
complete; no sleep or reconnect occurs. This library accepts values from zero
through `2**63-1`. A numeric value above that ceiling fails with `retry_limit`, a
library resource-profile rule, not a protocol requirement. Invalid or empty digit
fields are ignored. Conversion removes leading zeroes and checks size before
constructing an integer. Unterminated retry lines never take effect.

`[DONE]` remains ordinary data. SSE framing completion, a JSON document's syntactic
completion and a provider's successful generation termination are different events.

## State, failure and ownership

`feed()` returns `SSEBatch(events, snapshot)`. Events have zero-based sequence,
`event_type`, `data` and `last_event_id`. Snapshots expose counters, status, committed
ID and retry state. They are immutable reports, not accepted-input/deserialization
formats. Event values are real application text and may contain secrets; repr omits
those fields and error messages omit raw lines, but this is not memory erasure or
an exception-traceback redaction facility.

The creating thread owns mutation. Reentry and mutation from another thread fail.
Empty chunks count against budgets but are not EOF. `finish()` is explicit EOF,
returns no events and records `discarded_unterminated_line` and
`discarded_pending_block`. The latter includes unfinished comment/metadata blocks,
not only blocks containing data. Pending IDs do not overwrite the final committed
ID. A completed retry line can already have changed retry state without a blank line.

Successful feed calls deliver all messages in their batch. If a later part of the
same call fails, that call raises without returning its earlier parsed messages.
Internal counters/control state do not roll back; FAILED is terminal and cannot
resume. Consequently delivery on malformed/over-budget streams can depend on chunk
boundaries. Earlier successfully returned messages remain complete SSE messages,
but do not certify whole-stream success or semantic validity.

`close()` releases pending parse buffers and is idempotent. OPEN becomes CLOSED;
FINISHED or FAILED remains unchanged. Committed metadata and counters remain for
diagnostics. The decoder owns no iterator, task, file, socket or connection, so
their cancellation/cleanup belongs to the caller. It never invokes callbacks.

## Explicit resource bounds

| Limit | Default | Maximum configurable value |
|---|---:|---:|
| Total input bytes | 4,000,000 | 64,000,000 |
| Feed calls, including empty chunks | 100,000 | 1,000,000 |
| One decoded line's characters | 262,144 | 4,000,000 |
| Pending event data, including one LF per field | 1,000,000 | 8,000,000 |
| Data fields in one event | 10,000 | 100,000 |
| ID/event field characters | 1,024 | 16,384 |
| Completed lines, including ignored/blank lines | 200,000 | 2,000,000 |
| Dispatched messages | 10,000 | 100,000 |
| Cumulative output characters | 8,000,000 | 64,000,000 |
| Work units | 32,000,000 | 256,000,000 |

Each setting requires an exact positive integer, not a boolean. Input bytes and
chunk counts are admitted before UTF-8 decoding. Line characters are checked before
retention; data/field counts before buffering. Metadata bounds apply to processed
metadata values: NUL-containing IDs are ignored but still consume line/input/work
budgets. The fixed default type `message` remains seven characters even when the
configured field limit is smaller.

Cumulative output includes data, type and ID on every message, charging repeated
persistent IDs each time. Work is charged as one unit per feed/finish, one per
decoded codepoint (including BOM/CRLF), `1 + 4*line_length` before line joining and
field processing, and `1 + 4*output_characters` before message joining/validation.
These are conservative operation-budget units, not CPU instructions or token usage.
All these independent limits apply; reaching one can reject input below another.

Line/data buffers use append-and-join, never reparse or concatenate an accumulated
stream prefix. Parsing costs linear input/line work plus charged output work. The
UTF-8 decoder materializes each admitted chunk before per-character work checks;
line fragments, joined strings and returned events can coexist. This is bounded
object/work accounting, not a hard RSS or wall-clock guarantee. Caller retention,
serialization and transport buffering are outside the decoder's ownership.

## Offline JSON application example

Run `python examples/sse_json_output.py`. Its deliberately explicit application
protocol uses `json-fragment` messages and a final `document-complete` message.
It preserves inserted intra-event LF, adds nothing between event payloads, rejects
messages after completion and refuses semantic approval if EOF discards any tail.
Only after actual local EOF and application completion does it call the existing
`IncrementalOutputSession.finish()`. This is not a claim of provider interoperability.

HTTP status/content-type handling, response ownership, reconnect/backoff, resume
headers, provider schemas/tool calls/usage aggregation and network integration
remain open in the [whole-repository gap ledger](parity-validation.md).

## Verification snapshot

Windows Python 3.14.5 passed the final 1,727-test repository suite with RuntimeWarning
treated as an error, 98.33% combined statement/branch coverage and 100% in the new
framing module. Python 3.12.0 separately passed all 167 new tests. Tests include
all two/three-way byte splits, a separate whole-body oracle, random malformed UTF-8,
every limit boundary, pre-construction budget sentinels, control cleanup and the
offline application example. A read-only peer audit independently checked 700
seeded streams, including exact work accounting, with no mismatches. A separate
Encoding investigation compared plain UTF-8 plus the BOM gate against Node's
TextDecoder over 1,290 byte inputs / 7,897 two-chunk cases with no mismatches; this
was an offline development check, not a runtime dependency or HTTP test.

Ruff lint/format, strict Mypy, Bandit, lock consistency, package metadata/content
checks, demo comparison and an isolated built-wheel framing roundtrip passed.
These are local implementation results, not provider interoperability, remote CI
or whole-repository parity claims.
