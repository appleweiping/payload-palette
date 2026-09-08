# Backpressured asynchronous output consumption

`validate_async_output_chunks` accepts an `AsyncIterable[bytes]` and feeds the existing
strict incremental UTF-8/JSON session. It pulls one chunk, processes it, and awaits the
optional progress observer before requesting another. It creates no background tasks,
prefetch queue, thread or connection. A slow observer naturally delays the next pull.

```python
from payload_palette import AsyncOutputPolicy, validate_async_output_chunks

# source is an application-owned AsyncIterable[bytes]; pipeline is a ValidationPipeline.
result = await validate_async_output_chunks(
    source,
    pipeline,
    policy=AsyncOutputPolicy(timeout_seconds=30, cleanup_timeout_seconds=5),
    close_iterator=True,
)
if result.report.valid:
    accepted_value = result.report.output
```

`python examples/async_model_output.py` runs a complete offline example, including Unicode
code points split across chunks, a cooperatively awaited observer and generator cleanup.
It does not contact a model provider. An external adapter must explicitly translate its
transport's events into exact UTF-8 byte chunks and own any sockets/tasks it creates.

## Source, progress and completion

The helper obtains one iterator with Python's class-MRO/descriptor-bound `__aiter__`
semantics (ignoring instance method shadows) and consumes until an actual
`StopAsyncIteration`. Empty chunks are not EOF. The existing `IncrementalLimits` and
pipeline `OutputLimits` apply unchanged. Already-created source buffers and allocations
inside callbacks remain outside those budgets.

`on_progress`, when supplied, must return an awaitable that resolves to `None`. Ordinary
synchronous callbacks, async generators and wrong await results are rejected. Every
successfully consumed chunk produces one callback, including an empty event batch.
EOF-only number completion events remain in `result.final_events`, not in a fabricated
extra progress callback. On failure, there is no lookahead pull.

Observers see actual provisional syntax events and keys, **not schema validation or
semantic approval**. Events and callback/source exceptions are not privacy-redacted.
A later syntax, semantic, resource, timeout or cleanup failure invalidates provisional
observations; retained copies cannot be retracted by this helper. The complete bounded
value graph is retained until final validation, so this is not constant-memory parsing.
See [the incremental parser contract](incremental-output.md) for exact counters and
Unicode, numeric, duplicate-key and immutable snapshot semantics.

Once EOF is reached, explicitly owned iterator cleanup must succeed before
`session.finish()` invokes the complete semantic pipeline. Semantic rejection returns a
final result with `report.valid == False`; source/parser/observer/cleanup exceptions
propagate without producing an accepted result. Synchronous semantic validators remain
trusted; this helper does not turn them into asynchronous or per-field streaming rules.

## Ownership and cooperative deadlines

`close_iterator=False` is the default and does not close caller-owned iterators. Use
`True` only when transferring responsibility for the iterator returned by `aiter`.
If it has an `aclose` method, that method must return an awaitable resolving to `None`.
An iterator without `aclose` requires no explicit close. Iterator-construction failures
cannot transfer ownership of a result that was never returned; that source implementation
remains responsible for resources it acquired. Malformed awaitables/async generators
similarly remain their creator's responsibility; the helper does not guess how to drive them.
Known malformed native coroutine returns are closed without executing their bodies, including
bad iterator results, yielded chunks and nested observer/cleanup results. This does not cancel
arbitrary returned futures or own tasks created inside an adapter.

`AsyncOutputPolicy` has two finite positive duration limits, each at most 86,400 seconds:

| Limit | Default | Scope |
| --- | ---: | --- |
| `timeout_seconds` | 30 seconds | Monotonic deadline from after session/configuration setup through source acquisition, consumption, observations and final semantic validation |
| `cleanup_timeout_seconds` | 5 seconds | Separate grace beginning when explicitly owned cleanup is attempted, including close lookup/call/await |

The consumption timer uses `asyncio.timeout_at`. If consumption fails or times out,
owned cleanup still gets its separate grace. Thus cleanup may continue beyond the
consumption deadline, but it cannot turn a late result into approval. Successful cleanup
is followed by another deadline check before validation. Synchronous parsing/observation
work and final validation are also followed by monotonic deadline checks because a
blocked event loop cannot deliver its timer callback promptly.

These are **cooperative**, not hard wall-time bounds: trusted adapters/observers/validators
that block the loop or repeatedly suppress cancellation can exceed them. There is no
forced thread/process termination or detached cleanup worker. The function awaits the
operations it starts and never returns an approval merely because a timer was swallowed.
Sources and observers must not manipulate the task's cancellation count with `uncancel`.
An already-handled cancellation count is preserved; newly observed cancellation is
propagated and never deliberately cleared by this helper.
If a timer's cancellation is translated into an ordinary adapter error, its expired timer
still classifies the result as a timeout, with the adapter error retained as its cause.
New external cancellation takes precedence over such ordinary translated errors.

On an existing failure, ordinary cleanup exceptions add a generic note and preserve the
primary exception, including cancellation. A new cleanup control exception propagates.
Without a primary failure, cleanup failure blocks final validation. Cleanup timeout or
cancellation may leave an adapter resource open: this function does not claim cleanup
success when it could not verify completion. Inspect/retry through the adapter's explicit
lifecycle, never by assuming a returned provisional event was accepted.

Concurrent invocations own independent parser state. The caller remains responsible for
not concurrently advancing the same iterator. Since semantic validation can have trusted
external side effects, a timeout detected after validation does not roll those effects
back; callbacks should remain pure or explicitly idempotent.

## Remaining scope

This is an asynchronous consumer for the existing independently authored parser, not an
HTTP/SSE decoder, provider integration, streaming generate/re-ask runner, token accounting
protocol or remote billing guarantee. Transport framing, model adapters, asynchronous
semantic rules, incremental repairs and reference-comparable performance remain open.
No full Guardrails/Pydantic repository-equivalence claim follows from this lifecycle slice.
