# Asynchronous validated generation

`AsyncGenerationRunner` runs a provider-neutral complete-output lifecycle: request, validate,
optionally re-ask, and accept only the final validated result. It accepts an exact `ValidationPipeline`
or `AsyncValidationPipeline`;
the existing multimodal request parser and safety policy remain a separate boundary.

There are no model SDK dependencies, credential lookups, dynamic plugin imports, hidden network
requests, or hosted services. The caller explicitly supplies a trusted asynchronous provider.
The repository's example and tests use offline adapters:

```bash
python examples/generate_validated_output.py
python examples/generate_with_async_checks.py
```

## Provider protocol

Implement `async def generate(self, request: GenerationRequest) -> GeneratedResponse`.
The runner requires an actual async function, not a synchronous function returning a future.
`GenerationRequest` contains:

- `instruction`: the caller's bounded prompt, unchanged across attempts;
- `schema`: a fresh JSON Schema keyword snapshot from the configured `OutputSchema`;
- `attempt`: a one-based attempt index;
- `max_output_tokens` and `remaining_total_tokens`: per-request output guidance and remaining
  budget calculated from earlier reported usage;
- `max_response_bytes`: the smaller remaining per-response/whole-run byte budget, which the adapter
  should enforce before buffering a network response;
- `feedback`: bounded stable failure codes and declared schema field paths from the previous attempt.

Return `GeneratedResponse(text, TokenUsage(input_tokens, output_tokens))`. The response must contain
complete JSON text and explicit nonnegative integer usage. Provider-side SDK/body limits are still
necessary: the runner can reject an oversized returned string but cannot prevent an adapter from
allocating that string before returning it. The response dataclass itself caps text at eight million
Unicode scalar characters. Provider code is responsible for translating its SDK response to this
protocol and for reporting all usage accurately.

The instruction is sent only to this explicitly supplied provider. Requests never contain earlier
raw response text. Example adapters deliberately require no account or credentials; replacing them
with an external provider is an application integration decision.

## Lifecycle and errors

Each run has local immutable history and independent budgets. Model attempts and synchronous
bindings execute sequentially; an async pipeline runs bounded concurrent final checks after
the complete synchronous repair stage. The same runner can serve concurrent caller-created
tasks; the provider must support the concurrency the caller chooses. There is no implicit global
semaphore, shared budget, or cache across runs.

1. Validate the instruction and prepare the schema snapshot.
2. Check remaining time, reported-token, response-byte, and validator-call budgets.
3. Await the provider under the smaller attempt or remaining-total timeout.
4. Validate the response/usage contract, byte limits, and reported usage.
5. Strictly decode JSON and run schema and semantic validation, including final repair verification.
   With an async pipeline, await its reject-only checks against isolated final candidate copies.
6. Return an accepted report, or send bounded feedback in the next request if generated output is
   invalid and attempts/budgets remain.

Only content failures trigger a re-ask. Malformed JSON, schema violations and ordinary semantic
rejections can be corrected by a new response. Provider exceptions, malformed response contracts,
timeouts, exhausted budgets, malformed validators and invalid validator repairs terminate the run.
There is no implicit transient network retry. Every attempt that reaches the provider appears in the
history; exhaustion checked before another provider call adds no fictitious attempt.

Termination values are `accepted`, `attempts_exhausted`, `provider_error`, `provider_contract`,
`timeout`, `deadline`, `token_budget`, `response_budget`, `validator_budget`, and `validator_error`.
Per-attempt statuses additionally distinguish `invalid_output`. A valid JSON-null result has
`output is None`, so use `report.valid` to determine acceptance.

## Cooperative deadlines and cancellation

The attempt timeout surrounds the asynchronous provider await. The total deadline also includes
schema preparation, decoding and local validation; it is checked before requesting another
response and before acceptance. Slow local synchronous validators are not interruptible. They
may delay cancellation and exceed elapsed deadlines, but their late result is rejected before
acceptance. Use appropriately bounded trusted callbacks.

External task cancellation propagates as `asyncio.CancelledError`; it is not converted into an
ordinary failure report. The provider receives cancellation and can clean up in `finally`. Even
if an adapter catches cancellation and returns a response, the runner checks pending cancellation
and elapsed deadlines, preventing late acceptance. An adapter that never yields or ignores
cancellation indefinitely cannot be forcibly stopped by this API. The implementation provides
cooperative orchestration, not process isolation or hard real-time execution.

An internal expired deadline remains a timeout even if provider cleanup raises an ordinary exception.
Pending caller cancellation takes precedence over such cleanup errors. Ordinary cleanup errors from
a malformed coroutine returned in place of a response become sanitized `provider_contract` failures;
`BaseException` control-flow signals continue to propagate.

## Budgets and accounting

| Policy field | Default | Hard ceiling |
|---|---:|---:|
| `max_attempts` | 3 | 32 |
| `max_output_tokens` | 512 | 128,000 |
| `max_total_tokens` | 4,096 | 1,000,000 |
| `max_response_bytes` | 1,000,000 | 8,000,000 |
| `max_total_response_bytes` | 4,000,000 | 64,000,000 |
| `max_validator_invocations` | 1,000 | 100,000 |
| `max_feedback` | 16 | 100 |
| `max_instruction_characters` | 100,000 | 1,000,000 |
| `attempt_timeout_seconds` | 30 | 86,400 |
| `total_timeout_seconds` | 120 | 86,400 |

Reported input and output tokens are charged across every completed response, including invalid
outputs. A response is rejected if reported usage exceeds the total or requested output-token cap.
Exactly reaching the total can still accept a valid final result. The next request's output-token
hint is reduced to the remaining reported budget. Input tokens/prefill are known only after the
provider returns, and cancelled/failed providers may have consumed unreported tokens. Consequently
these controls do not guarantee a remote provider's monetary billing limit.

`report.reported_tokens` contains known usage and `usage_complete` is false when provider usage
was unavailable, including provider failure, malformed contracts and cancelled provider deadlines.
Local post-response validation deadlines preserve known usage. Reports do not fabricate zero-cost
usage for failed providers.

Response limits count UTF-8 bytes, including multibyte characters. Per-response and aggregate limits
are both checked. Scalar lengths are measured before allocating the UTF-8 byte copy, so a multibyte
body cannot force an oversized encoding allocation. `response_bytes` records measured UTF-8 bytes,
including a measured body rejected for exceeding the cap. It is zero for a body rejected by the
character-count lower bound or before body processing; it is not a measurement of remote traffic.
Validator invocation budgets include initial, fix-check, and final-verification phases and remain
shared across attempts. The wrapped pipeline's own per-attempt limit also remains in force.
Existing synchronous counts charge admission before callback lookup/snapshot; asynchronous counts
charge actual check-factory entries. A failed getter therefore charges one synchronous admission
but zero asynchronous entries. Reserved async jobs cancelled before entry are not reported as
calls. Missing optional paths consume neither. These counters retain their existing meanings.

## Asynchronous semantic checks and re-asking

Pass an existing `AsyncValidationPipeline` directly to the runner. Its synchronous pipeline repairs
and verifies the candidate exactly once per response; asynchronous validators then observe that
final candidate, never an intermediate repair. The existing concurrency and per-check policies
apply. All checks settle before acceptance, re-asking, or propagation of a control exception.
The async generation integration makes one additional bounded JSON boundary copy before the
synchronous pipeline. Only decoding and this pre-callback snapshot produce structural re-ask
errors. Same-class exceptions escaping later schema/repair/scheduler work propagate unchanged,
without a retry or fabricated partial invocation count. This copy shares the existing limits and
absolute deadline; it does not replay any repairs or validator callbacks. Standalone validation
and the original direct synchronous generation path do not add this copy.
Completion order cannot reorder declared-rule feedback. See [the async validation contract](async-validation.md)
and the [offline executable example](../examples/generate_with_async_checks.py).

The provider attempt timeout still ends when its complete response arrives. Validation uses the
earlier of its policy's total deadline and the generation run's remaining absolute deadline;
equality belongs to generation. The existing validation owner performs cancellation and awaited
cleanup, without an additional generation timeout racing it. A local timeout whose cleanup crosses
the generation deadline becomes `timeout` / `deadline`. A genuine control still propagates after
settlement. Cooperative cleanup may exceed either deadline or never finish if trusted code resists
cancellation; this is not hard preemption, process isolation, or a remote billing guarantee.

Final accepted-output copying and report encoding also count against the async
generation deadline. The runner stages that result once and checks cancellation
and the deadline again before returning it. If this work reaches the deadline,
the attempt is `timeout`, termination is `deadline`, and output is absent; known
usage, response bytes and settled invocation counts are preserved. There is no
second output encoding or validator replay on that path. Encoding failures and
genuine controls still propagate as failures, not fabricated successful reports.

For the async path, trusted scheduler causes are separate from caller-supplied diagnostic codes:

| Event | Attempt / termination |
| --- | --- |
| Schema or ordinary semantic rejection | `invalid_output`, re-ask if budgets permit |
| Aggregate validation invocation exhaustion | `validator_budget`, no re-ask |
| Per-check timeout, malformed callback/result, local validation total deadline | `validator_error`, no re-ask |
| Generation total deadline, including late validation settlement | `timeout` / `deadline`, no re-ask |
| Caller cancellation or genuine control | Propagate after owned cleanup; no synthetic report |
| Unexpected infrastructure failure without a completed report | Propagate after cleanup; no inferred partial count |

A user rejection named `validator_budget` or `validation_deadline` remains ordinary rejection.
A full issue/feedback prefix cannot hide a runtime-owned budget/deadline. When no generation calls
remain and either stage declares rules, admission stops before another provider request, even if
an optional path might have been absent. Rule-free pipelines need zero calls. Output/report wire
fields, provider protocols, and the original direct synchronous-pipeline behavior are unchanged.

This integration does not add async repairs, streaming/SSE generation, provider SDKs, field-only
regeneration, workers, hosted services, CLI/config interchange, or full reference-repository parity.

## Privacy and reproducibility

History records attempt number, status, known usage, measured response bytes, callback count, and
bounded feedback. It retains neither instructions nor raw rejected responses nor validator/error
message prose. Feedback paths are accepted only if they traverse declared schema properties/arrays;
unexpected generated keys collapse to `$`. Diagnostic codes and declared schema field names are
trusted configuration and must themselves be suitable for the intended logging destination.

`report.to_dict()` omits accepted output by default; `include_output=True` is explicit. `output`
returns a fresh copy, and raw response text/instructions are excluded from dataclass representations.
This is data minimization, not encryption or redaction of the final accepted output requested by a
caller. Supplied providers and validators can retain their own data outside this library.

Offline tests exercise invalid-JSON/schema/semantic re-asks, final repair checks, cancellation and
cleanup, cancellation suppression, late responses, shared budgets, provider/validator faults,
privacy-preserving feedback, concurrent run isolation and immutable snapshots. There are no live
provider benchmark claims or external model quality results in this slice.

See the [final acceptance record](parity-validation.md#asynchronous-semantic-generation-acceptance-2026-09-12)
for full-suite, independent installed-platform and preserved-baseline evidence.
