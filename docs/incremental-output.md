# Incremental structured output

`IncrementalOutputSession` consumes exact UTF-8 byte chunks using a persistent decoder and
finite-state JSON tokenizer/container parser. It does not repeatedly parse accumulated prefixes.
Completed JSON values produce immutable syntax events as soon as their terminator is known.
Only explicit document EOF calls the complete schema/semantic pipeline.

```python
from payload_palette import IncrementalOutputSession, OutputSchema, ValidationPipeline

pipeline = ValidationPipeline(
    OutputSchema("object", properties={"label": OutputSchema("string")}, required=("label",))
)
session = IncrementalOutputSession(pipeline)
first = session.feed(b'{"label":"ca')
assert first.events == ()
second = session.feed(b't"}')
assert second.statistics.root_complete  # Syntax only; later trailing content could fail.
result = session.finish()  # Assert real EOF, then validate once.
assert result.report.valid
assert result.report.output == {"label": "cat"}
```

## Completion, failure and privacy

Every `CompletedJSONValue` contains a zero-based completion index, exact path, exclusive decoded
character endpoint and `JSONValueSnapshot`. Arrays/objects finish after their closing delimiter;
their child snapshots are shared by identity. Keys do not get separate value events. JSON numbers
finish on a legal delimiter or EOF, so feeding `b"1"` emits no event until a delimiter or `finish()`.
The EOF number event is in `IncrementalResult.final_events`, not an earlier progress callback.
Offsets count Unicode code points in the JSON text, not bytes or decoded string values.

Events are **provisional syntax completion only**: neither schema validity nor semantic approval,
even for a complete root. A later invalid byte, duplicate key, trailing token, resource failure or
pipeline exception invalidates all previous provisional observations. Consumers must retract or
discard them; the parser cannot retract copies already retained by a caller. Semantic repairs at
finish do not rewrite earlier snapshots. Use only a valid final `report.output` as accepted data.

The session starts `OPEN`. A syntax/resource or pipeline exception makes it terminal `FAILED` and
releases retained parse data. Valid syntax followed by schema/rule rejection instead returns a
terminal `FINISHED` result with `report.valid == False`. `close()` abandons an open session as
`CLOSED`; it is idempotent and does not replace a finished/failed status. Counters remain available
after cleanup; `root_complete` records syntax history, not approval. Stateful calls require the
creating thread and reject reentry. Terminal sessions cannot be fed, finished again or reset.

Events expose **actual generated values and object keys**. They are not the privacy-minimized
history used by `AsyncGenerationRunner`. Some payload fields are hidden from default repr, but
that is not redaction: event paths, explicit accesses and `to_python()` copies can expose secrets.
No telemetry is sent. `IncrementalJSONError` itself includes only a stable code/character offset;
its chained decoder exception, trusted callback errors or source errors are not sanitized. Review
what you log, retain or share. Public record constructors check structure, not authenticity or
proof that a record really came from a session.

## Strict JSON and whole-document equivalence

Input chunks must be exact `bytes`, not text, bytearray or memoryview. Empty chunks count against
chunk/work budgets but are not EOF. UTF-8 may split inside a multibyte code point; string escapes,
paired escaped surrogates, number exponents and escaped object keys may split at any byte.
Only JSON's four whitespace characters (space, tab, LF, CR) are accepted outside strings.
BOMs, invalid UTF-8, raw/escaped isolated surrogates, duplicate decoded keys, nonfinite numbers,
leading zeros, incomplete numbers, trailing commas and additional root values fail. There is no
truncation guessing, Markdown extraction, partial acceptance or synthesized closing punctuation.

An unfinished UTF-8 sequence at EOF raises `incomplete_utf8`; an otherwise syntactically unfinished
document raises `incomplete_json`. Already impossible syntax fails during `feed`. A whole admitted
chunk is UTF-8-decoded before its characters enter the grammar, so invalid UTF-8 later in that
chunk can prevent events for its valid prefix. Event batch grouping and earliest diagnostic choice
are not invariant under chunking for invalid documents; successful final values are.

Within configured parser/output budgets, the final report matches
`pipeline.validate_json_bytes(complete_bytes)` for the same trusted deterministic validators.
The parser uses the existing strict JSON number interpretation: integers have at most 256 decimal
digits and floats must be finite. The complete pipeline additionally bounds integer bit length
to 850, so some syntactically valid 256-digit integers raise `OutputContractError` at finish, just
as they do through the whole-document API. Stricter incremental budgets can reject otherwise
accepted whole documents. Error classes/messages and rejection timing are intentionally distinct.
The existing [schema/rule ordering and final revalidation](output-validation.md) remain unchanged.

## Resource accounting

`IncrementalLimits` adds the following strict positive-integer budgets. Booleans are not integers
for configuration purposes. Pipeline `OutputLimits` still bound nodes, depth and total decoded
string/key characters before the completed document reaches callbacks.

| Incremental budget | Default | Hard ceiling |
|---|---:|---:|
| Total input bytes | 4,000,000 | 64,000,000 |
| Chunks, including empty chunks | 100,000 | 1,000,000 |
| Characters in one lexical token, including quotes/escapes | 4,000,000 | 64,000,000 |
| JSON lexical tokens, including punctuation | 100,000 | 1,000,000 |
| Parser/progress work units | 32,000,000 | 256,000,000 |

Nodes count JSON values, not object keys. Root depth is zero: an empty root container has depth
zero, and 65 nested empty containers have depth 64. Decoded characters count all object keys and
string values; an escaped surrogate pair contributes one character. These agree with `OutputLimits`.
Lexical token counts are unrelated to model tokens or provider billing.

Work charges one unit per chunk, character-state dispatch and finish call; a number delimiter
requires one extra dispatch. Container freezing charges its immediate child count, and every
event charges one plus its path length plus the characters in its string path segments. That
last cumulative charge bounds path-storage amplification, not just document size. String parts
are joined once, completed subtrees are shared without recursively copying each prefix, and UTF-8
decoder state retains only an incomplete code point between calls.

This is **not constant-memory validation**: the bounded complete value graph remains until finish,
and a feed temporarily owns its decoded chunk plus new events. Finish makes one mutable document
copy and the existing pipeline makes its own validation/repair snapshots. Observers may retain
events indefinitely or repeatedly call `snapshot.to_python()`; those caller allocations/work are
outside parser budgets. Work units bound documented operations, not wall time or exact RSS; Python
allocation, token conversion, Unicode checks and trusted validators are not a process sandbox.

## Pull sources and cleanup

```python
from payload_palette import validate_output_chunks

result = validate_output_chunks(
    [b'{"label":', b'"cat"}'],
    pipeline,
    on_progress=lambda progress: None,
    close_iterator=False,
)
```

The default does not own or close the caller's iterator. Set `close_iterator=True` only when
transferring cleanup responsibility for the iterator returned by `iter(chunks)`. If it has a
`close()` method, cleanup runs after consumption or failure and **before semantic validation**.
Cleanup must be synchronous; awaitable and async-generator results are rejected, and actual returned
coroutines are closed without executing asynchronous work. This helper cannot drive asynchronous
cleanup of custom awaitables or already-started async generators; those malformed trusted source
implementations remain responsible for their resources. The synchronous helper does not adapt async iterables.
The separate [asynchronous consumer](async-output.md) supplies awaited backpressure and explicit async cleanup;
neither helper supplies a provider-specific transport adapter.
If iterator creation itself fails, any resource acquired by that caller code remains its responsibility.

Observers must be synchronous and return `None`. They receive each feed's progress, including empty
batches; EOF-only events remain in the final result. Consumption stops on a parser/observer/source
error, with no extra pull. On a primary failure, an ordinary cleanup failure adds a generic note and
preserves the primary exception (including KeyboardInterrupt/SystemExit). A genuine new cleanup
control exception propagates. Without a primary failure, cleanup failure blocks finish/approval.
The same synchronous-close rule applies when cleanup lookup itself raises or a malformed close
method returns a coroutine. Arbitrary source/observer code remains trusted and cannot be preempted.

Run `python examples/incremental_model_output.py` for an offline provider-style chunk source,
Unicode boundary splits, provisional event counting, final repairs and owned-generator cleanup.
It makes no model/network request. No CLI flag silently changes the existing media-ingress
`--stream` contract, which is a separate normalization API.

## Reference comparison and open scope

Frozen first-party contracts reviewed for this slice:

- [Guardrails structured streaming guide](https://github.com/guardrails-ai/guardrails/blob/06d0ff2c5f9bcb493d976b76f885e37e41ce845d/docs/how_to_guides/streaming_structured_data.ipynb)
  documents yielding validated fragments through model integrations.
- [Pydantic JSON parsing guide](https://github.com/pydantic/pydantic/blob/2261ae19e2e09f792f06613360c83fc829238111/docs/concepts/json.md)
  documents optional partial parsing/default handling as well as complete parsing.

This API implements neither partial-document acceptance nor incremental semantic repair. Async
incremental provider integration, per-field/stateful streaming validators, backpressure protocols,
partial-schema status, reference-comparable performance and compiled acceleration remain open.
The [repository assessment](parity-validation.md) remains open; this tested parser is one slice,
not whole Guardrails/Pydantic parity.
