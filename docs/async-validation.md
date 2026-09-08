# Asynchronous final semantic validation

`AsyncValidationPipeline` adds real awaited, bounded-concurrency semantic checks to a complete
output. It composes with `ValidationPipeline`; it does not duplicate that pipeline's repair
engine or replace the synchronous API. The asynchronous stage is read-only and **reject-only**.
No provider SDK, network client, credential, worker process or dependency is introduced.

```python
from payload_palette import (
    AsyncRuleBinding,
    AsyncValidationPipeline,
    OutputSchema,
    RuleResult,
    ValidationPipeline,
)


class Allowed:
    async def check(self, value, context):
        return (
            RuleResult(True)
            if value in {"photo", "diagram"}
            else RuleResult(False, "unknown_label", "label is absent from the local catalog")
        )


pipeline = AsyncValidationPipeline(
    ValidationPipeline(OutputSchema("string")),
    (AsyncRuleBinding("catalog", (), Allowed()),),
)
# In an async application:
# report = await pipeline.validate("photo")
```

Run `python examples/async_semantic_validation.py` for a complete credential-free example using
two cooperative local catalog checks, a synchronous trim repair and an explicit rejected result.
The example's scheduling checkpoint is not a remote lookup or a performance benchmark.

## Two deliberately different stages

1. The total deadline starts before strict byte decoding, if requested, and before the complete
   synchronous pipeline. Existing structural validation, fix/filter actions, immediate repair
   checks and whole-document final verification remain unchanged. A rejected synchronous report
   schedules no asynchronous checks. Synchronous work cannot be preempted; late completion is
   rejected rather than approved after the deadline.
2. On success, resolve asynchronous binding paths against the final candidate. Each present
   rule receives its own bounded JSON snapshot and a `RuleContext` with `phase="final"`.
   Execute up to the configured concurrency and await their actual results.
3. Accept only when every applicable check passed and no deadline, cancellation, budget or
   contract failure occurred. The accepted serialized output is the synchronous pipeline's
   already-verified candidate, not callback-mutated data. All owned tasks settle before return.

Async validators cannot suggest repairs or filters. A `RuleResult` containing a `fix` is a
`validator_contract` error, even if the proposal would otherwise be valid JSON. Use the existing
synchronous repair stage for deterministic transformations. Overlapping rule paths are allowed
because the asynchronous stage cannot change the candidate. There are no concurrent mutation,
last-writer-wins, wildcard, incremental-field or partial-approval semantics.

## Binding, return and ownership contracts

`AsyncOutputValidator.check(value, context)` must be a declared `async def` method. Configuration
uses trusted local Python instances, never strings to import or evaluate. IDs are bounded lowercase
identifiers and must be unique across both stages. The combined binding count is at most 256.
Paths use the existing exact tuple format, at most 32 bounded key/index segments. Construction
rejects impossible schema paths; genuinely missing optional/array/union paths skip as before.

At runtime, calling the method must return a **fresh, unstarted native coroutine**. That return
transfers ownership to this run. Borrowed/pre-existing `Task` or `Future` objects, generic custom
awaitables and already-started coroutines are rejected without being awaited, canceled or closed.
The caller retains their ownership. A native coroutine that resolves to another coroutine instead
of `RuleResult`, or exposes a coroutine directly in `RuleResult.fix`, is invalid; the known nested
coroutine is closed without awaiting it. A previously started nested coroutine may execute cleanup
while closing. This includes malformed result fields and callbacks that swallow cancellation before
returning that resource. Arbitrarily nested callback resources and custom protocol internals are
not traversed or managed.

Normal results must be exact valid `RuleResult` objects. Each check receives an independent snapshot;
mutating it does not change input, another callback's copy or the accepted output. Validators must
still be deterministic in their decision and manage their external side effects. Snapshot isolation
does not prevent trusted Python code from accessing global objects, manipulating task internals,
performing I/O or allocating its own memory.

## Scheduling and budgets

`AsyncValidationPolicy` has the following exact configuration:

| Setting | Default | Accepted values |
|---|---:|---|
| `max_concurrency` | 4 | Built-in integer, 1–32; not bool |
| `per_validator_timeout_seconds` | 10 | Built-in int/float, finite, greater than 0 and at most 86,400 |
| `total_timeout_seconds` | 30 | Same numeric interval |

The scheduler admits present bindings in declaration order. It reserves from the **remaining**
`pipeline.limits.max_invocations` after all synchronous initial/repair/final calls. Reservation is
not invocation: `OutputReport.invocations` counts actual callback factory calls, not a queued job
that timed out before entry or whose method lookup failed. A reserved check canceled before entry
still consumed that run's admission slot; it is not replaced or retried. On budget exhaustion,
already-admitted work settles and the run is rejected explicitly. Missing optional paths use neither
a reservation nor an invocation.

Only up to `max_concurrency` callback tasks and fresh snapshots are live at once; the scheduler does
not eagerly materialize a snapshot or launch a task for every binding. At most 256 job/result records
are retained. Each snapshot reuses the existing depth/node/character/type limits. Snapshot work is
bounded by admitted jobs times those limits, not charged to the schema interpreter's separate
per-pass step budget. The complete candidate and synchronous report also remain in memory. These
are separate payload/count bounds, **not** an RSS sandbox or constant-memory validation claim.

Results are reported in binding declaration order, regardless of completion order; synchronous
outcomes precede asynchronous ones. Actual launch/completion times and callback side effects are
not promised to be reproducible. Existing issue/message limits apply. A deadline can leave rules
unstarted or canceled without a result; the rejected report is not evidence that every rule ran.

## Deadline, cancellation and cleanup semantics

The total elapsed budget includes decoding, synchronous validation, the accepted-output copy,
callback snapshots, asynchronous execution and bounded outcome/path construction. Per-validator
timing starts when its owned worker begins, including its snapshot, not while waiting for an
admission slot. Late synchronous callback bodies are rejected even if they never yield to an
event-loop timer.

Only the parent owns total-deadline cancellation. Per-check timers are disarmed before parent
cleanup begins, and a child already canceling is not canceled again. This prevents competing timers
from interrupting an awaited `finally`. Repeated caller cancellation is preserved while the parent
shields and drains owned child settlement; it does not repeatedly cancel those children or reduce
the caller's cancellation count. A callback that swallows or translates cancellation cannot produce
an accepted report merely by returning success.

Child `KeyboardInterrupt`, `SystemExit` and other genuine control exceptions are carried internally
until siblings settle, then raised in the parent. A genuine cleanup control takes precedence over
an ordinary infrastructure failure; if both are controls, the first observed control is preserved.
An external caller cancellation is not converted into an ordinary rejected report. Internal
deadline failures are rejected reports. There is no detached/background-task escape: trusted code
that blocks the loop or resists cancellation can delay cleanup and return indefinitely. Deadlines
are cooperative, not hard execution/cleanup-time guarantees.

## Reports, errors and privacy

The return type is the existing immutable `OutputReport`. Accepted JSON null and rejected output
both have `output is None`; always inspect `valid`. Async outcomes have phase `final` and statuses
`passed`, `rejected` or `error`. Stable runtime codes are `validator_contract`, `validator_timeout`,
`validation_deadline` and `validator_budget`. Callback-provided codes do not control the scheduler,
even when spelled the same way as a runtime code.

Ordinary callback/return-contract exceptions are sanitized without copying exception prose.
Unexpected local scheduling failures propagate only after child cleanup. Invalid Python JSON or
snapshot admission retains `OutputContractError`; strict byte syntax/ingress errors retain the
existing `PayloadValidationError`/configuration error boundary. `validate_json_bytes` does not
accept partial JSON or disable decoding through a special invalid byte-limit value.

The report omits output by default but is **not a redactor**: validator messages, paths and requested
output may contain sensitive data. No logger, network exporter or persistent history is installed.
The generation runner and incremental byte consumers still accept their existing synchronous
pipeline type; this API does not silently change their cancellation, repair or provider contracts.

## Reference scope and verification

The frozen [Guardrails runtime inventory](https://github.com/guardrails-ai/guardrails/blob/06d0ff2c5f9bcb493d976b76f885e37e41ce845d/guardrails-features-list.md)
identifies asynchronous validation and parallel field execution as public capabilities. This
original local implementation adds that bounded reject-only final stage, not its integrations,
distributed executors, asynchronous corrective actions or wire compatibility. The
[whole-repository assessment](parity-validation.md) remains open.

Tests exercise real Event-coordinated overlap and reverse completion, independent synchronous/
asynchronous reject-rule comparisons, final repair isolation, declaration-order shared budgets,
task ownership, strict malformed results, two timer owners, late synchronous work, suppressed and
repeated cancellation, genuine cleanup controls and simultaneous independent runs.

Final local verification on Windows passed **1,824 tests** on Python **3.14.5** (119.17 s) and
**3.12.0** (74.58 s), with no skips and RuntimeWarning treated as an error. The 3.14 run passed
the unchanged 95% combined statement/branch coverage gate at **98.36%**; the 3.12 run did not
collect coverage. All **97** new focused cases passed independently on both interpreters.
The new module reached **98.90%**, with only two defensive internal invariant guards uncovered.

The final timing fixtures use entry/settlement barriers and a controlled loop clock where the
ordering itself is under test. Earlier 10 ms fixture assumptions were not portable to Windows
3.12's coarse clock and were corrected; production budgets were not increased. Separate real
wall-clock tests still verify rejection, actual invocation accounting, awaited callback cleanup
and absence of leftover child tasks. Local test counts do not establish throughput equivalence,
hard deadline enforcement, remote CI success or whole-reference parity.

Ruff lint/format, strict Mypy (24 source modules), Bandit, frozen-lock consistency and whitespace
checks passed. All seven offline output examples and the existing generated-demo comparison also
passed; the async example verifies a trimmed accepted classification and a rejected local label.
