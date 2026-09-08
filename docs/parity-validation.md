# Reference repository assessment: validation

Assessment date: 2026-09-07. Overall full-repository parity status: **OPEN**.

The references below are comparison evidence. Payload Palette's new implementation was written
locally without copying their implementation. A similar public API category or a passing local
test does not establish equivalent breadth, runtime performance, ecosystem compatibility, or
repository scale.

## Frozen first-party references

- [Guardrails 06d0ff2c5f9bcb493d976b76f885e37e41ce845d](https://github.com/guardrails-ai/guardrails/tree/06d0ff2c5f9bcb493d976b76f885e37e41ce845d),
  commit timestamp 2026-08-26T19:53:51Z. Its own
  [feature inventory](https://github.com/guardrails-ai/guardrails/blob/06d0ff2c5f9bcb493d976b76f885e37e41ce845d/guardrails-features-list.md)
  enumerates schema/configuration, synchronous and asynchronous validation, corrective actions,
  model integrations, streaming, history/telemetry, and server capabilities. The feature list
  is the upstream author's inventory, not independent proof that every listed feature works.
- [Pydantic 2261ae19e2e09f792f06613360c83fc829238111](https://github.com/pydantic/pydantic/tree/2261ae19e2e09f792f06613360c83fc829238111),
  commit timestamp 2026-09-07T13:15:01Z. Its
  [API documentation tree](https://github.com/pydantic/pydantic/tree/2261ae19e2e09f792f06613360c83fc829238111/docs/api)
  and [implementation tree](https://github.com/pydantic/pydantic/tree/2261ae19e2e09f792f06613360c83fc829238111/pydantic)
  expose models, field configuration, validators/serializers, type adapters, dataclasses, call
  validation, JSON Schema, aliases, network/standard types, and a separate core implementation.

GitHub's recursive tree API returned `truncated: false` for both frozen commits. The inventory
counts all tracked blobs, including fixtures and generated/compatibility files; it is not LOC:

| Reference | Tracked blobs | `.py` files | Files under `tests/` | Files under `docs/` |
|---|---:|---:|---:|---:|
| Guardrails | 563 | 354 | 257 | 73 |
| Pydantic | 828 | 442 | 226 | 167 |

## Gaps and acceptance work

`Partial` means a specific tested subset exists. `Open` means it is not implemented here. None of
these rows has been promoted to whole-reference equivalence.

| Capability | Current evidence | Status / remaining acceptance work |
|---|---|---|
| Strict nested structured JSON output | `OutputSchema`, `tests/test_output_schema.py`, `tests/test_schema_interchange.py` | Partial: JSON types, homogeneous arrays, typed maps, properties, required/extra keys, scalar enums, numeric/length bounds, bounded anyOf/nullability. Recursive references, discriminators and full schema dialect test corpus remain open. |
| Schema generation and Python type system | `AnnotationAdapter`, `DataclassAdapter`, `schema_for_annotation`, annotation/dataclass/scalar tests | Partial: strict primitives, list/dict, unions, Literal, TypedDict, Annotated constraints, bounded dataclass construction/defaults/serialization and twelve concrete temporal/Decimal/UUID/IP scalar types. Generic models, forward references, broader model/validator semantics, callable validation, aliases and other standard type families remain open. |
| Validator composition and failure actions | `OutputValidator`, `RuleBinding`, `ValidationPipeline`, `tests/test_output_validation.py` | Partial: synchronous exact-path reject/fix/object-filter actions, immediate repair check and final verification. Wildcard dispatch, broader action semantics and interoperable validator serialization remain open. |
| Model invocation and re-asking | `AsyncModelProvider`, `AsyncGenerationRunner`, `tests/test_generation.py` | Partial: provider-neutral complete-output lifecycle, bounded schema/semantic re-asks, explicit usage and offline failure tests. Live provider adapters, provider interoperability, richer field regeneration and generation-quality evaluations remain open. |
| Async and concurrency | Actual asynchronous provider protocol; `AsyncValidationPipeline` with owned coroutines, bounded final-field scheduling, deadline/cancellation and isolation tests | Partial: asynchronous reject-only final semantic checks compose with synchronous repairs under shared invocation budgets. Async repair/filter scheduling, provider/stream integration of the new stage, isolated workers and distributed execution remain open. |
| Incremental output validation | `IncrementalOutputSession`, synchronous/async chunk consumers and their regression suites | Partial: finite-state UTF-8/JSON parsing, immutable provisional complete-value events, EOF validation and backpressured async consumption with cooperative cancellation and explicit cleanup. Incremental semantic rules, provider transport adapters and reference performance remain open. |
| Model/tool/schema integration | Existing request envelope adapters normalize multimodal input | Open: output tool-call/schema contracts, schema-driven generation, provider adapter verification. |
| Runtime schema import and rich serialization | `load_output_schema`, `export_output_schema`, configured `AnnotationAdapter.dump_json`, strict JSON ingress | Partial: strict bounded keyword import/export, immutable snapshots and exact-byte JSON presentation options. Full dialects, pipeline interchange, version migration, custom serializers and broad cross-engine interoperability remain open. |
| History, metrics, tracing and privacy | Bounded immutable rule outcomes and generation/re-ask history; raw output/instruction/validator prose excluded from generation history | Partial: local per-attempt token/response/callback accounting and declared-path feedback. Persistent lineage, richer redaction policy, execution metrics and opt-in trace exporters remain open. |
| CLI / deployment | Existing request CLI; executable `examples/validate_model_output.py` | Partial: output validation CLI with declarative configuration, service API, deployment examples and service-level tests remain open. |
| Performance / implementation scale | Dependency-free Python implementation; existing ingress benchmarks | Open: representative schema/validator benchmarks, first-party reference differential measurements, compiled acceleration decisions and independently audited scale inventory. |
| Repository engineering | Existing CI, type checking, security scan, coverage gate and release workflows | Partial: local gates validate this slice, not reference-level completeness. Add cross-version/interoperability corpus and reproducible performance evidence before parity closure. |

## This implementation slice

The concrete delivered slice is local complete-output schema validation plus a synchronous semantic
pipeline. It includes an executable classification example using trim/case repairs and optional
field filtering. Tests check wrong schemas, malformed callbacks/results, failed repair, required
field deletion, later repairs invalidating earlier validators, duplicate raw JSON keys, mutation
isolation, cycles, strict numeric/Unicode semantics, and per-call/aggregate resource limits.

The second slice adds a real asynchronous provider protocol and complete-output generation/re-ask
orchestration. It applies cooperative deadlines, cancellation propagation, shared reported-token,
UTF-8 response and callback budgets, with privacy-minimized feedback/history. Offline tests cover
independent concurrent runs, provider/validator faults, late output, cancellation suppression and
cleanup exceptions, malformed provider usage, Unicode byte allocation, and huge-integer timeout
configuration. Independent review also added regressions for impossible closed-schema binding paths,
internal deadline cleanup failures and malformed-coroutine cleanup errors. The contract explicitly
excludes hard callback preemption and provider billing claims.

The third slice adds strict runtime schema data import/export, bounded unions/nullability and typed
additional properties, trusted annotation compilation and configured JSON serialization. Tests cover
independent keyword vectors, unknown keys, malformed definitions/annotations, snapshot isolation,
cycles, branch/depth/node/character/work budgets, exact Unicode byte caps, and pipeline/generation
integration. JSON Schema integer semantics are tested separately from strict Python annotations;
Payload's interchange extension preserves that otherwise nonportable representation distinction.
This is not full Pydantic type support or a full JSON Schema interpreter.

The fourth slice adds genuine incremental UTF-8/JSON parsing without repeated prefix decoding,
with shared immutable complete-value events and final complete-pipeline validation. Differential
tests compare every byte split of strict syntax vectors, seeded nested documents and existing
whole-document reports. Additional tests cover source/observer/cleanup failures and control
exceptions, Unicode/number boundaries, provisional invalidation and explicit byte/token/path-work
limits. This retains the complete bounded value graph and does not claim constant memory or
incremental semantic validation. See the [exact incremental contract](incremental-output.md).

The fifth slice adds a genuine asynchronous iterator consumer with awaited observers (no
prefetch), a cooperative deadline and separately bounded owned cleanup before semantic approval.
Tests exercise real cancellation/timeouts, swallowed cancellation, retained prior cancellation
counts, late synchronous callbacks, cleanup/control failures and concurrent consumer isolation.
It reuses the parser and complete pipeline, not a second parsing/validation implementation.
Provider framing, token accounting and incremental semantic rules remain open.

Run the same commands used for repository CI:

```bash
uv run --frozen --extra dev python -m ruff check .
uv run --frozen --extra dev python -m ruff format --check .
uv run --frozen --extra dev python -m mypy
uv run --frozen --extra dev python -m bandit -q -c pyproject.toml -r src
uv run --frozen --extra dev python -m pytest --cov=payload_palette --cov-branch --cov-report=term-missing
uv run --frozen --extra dev python examples/validate_model_output.py
uv run --frozen --extra dev python -m build --no-isolation
```

Full-repository parity can close only after the open work has explicit implementation, meaningful
independent tests, documentation, performance/scale evidence, and reviewed reference differences.
The presence of this assessment does not itself satisfy those gates.

## Historical third-slice verification snapshot

On Windows / Python 3.14.5, the completed suite passed **1198 tests** with **97.95% combined
statement/branch coverage**. Python 3.12.0 independently passed the same 1198 tests. Schema and
annotation-adapter modules reported 100% coverage; schema interchange and pipeline reported 99%,
and asynchronous generation reported 97%. Ruff lint and formatting, strict Mypy, Bandit, all three
executable output examples, the existing demo comparison, wheel/sdist build, Twine checks,
wheel-content checks and isolated built-wheel schema/annotation smoke passed. This is local
evidence; remote CI for this uncommitted implementation has not been run or claimed.

The local source inventory has 20 Python files (5748 nonblank lines); tests have 20 Python files
(4879 nonblank lines), counted with `rg --files` and PowerShell `Measure-Object -Line`. These counts
include existing ingress functionality and the new output slice. They remain substantially below
the reference repository inventories above; test count and coverage do not erase that scope gap.

## Incremental-slice local verification

Windows Python 3.14.5 and Python 3.12.0 each passed **1450 tests** with RuntimeWarning promoted to
an error. The 3.14 run reported **98.16% combined statement/branch coverage**; the incremental module
reported 99%, with only three internal representation-invariant guards uncovered. The new module
contributes 235 collected regression cases, including parameterized cases and seeded/all-split
corpora within those tests. Counts describe the actual local suite, not reference equivalence.

Ruff lint/format, strict Mypy, Bandit, all four output examples, the existing demo comparison,
wheel/sdist build, Twine, wheel-content checks and an isolated installed-wheel incremental smoke
passed. Remote CI for this uncommitted slice has not been run or claimed. The initial lock check
also exposed a pre-existing package-metadata mismatch: the committed lock recorded 0.3.0 while
the project metadata recorded 0.5.0. The single stale lock metadata line was synchronized to the
existing project version, with no dependency upgrade or release-version bump. The repeated
`uv lock --check` then passed.

## Async-consumption local verification

Final Windows Python 3.14.5 ran 1,560 tests with RuntimeWarning treated as an error:
all passed, with 98.23% combined statement/branch coverage and 100% in `async_output.py`.
Python 3.12.0 independently passed all 110 new focused cases. An initial 3.12 test run
exposed nonportable `cr_frame is None` assertions: that interpreter can retain the frame
after `coroutine.close()`. The final tests check the observable prohibition on reuse
without executing the coroutine body instead; the cleanup implementation was not weakened.

Independent lifecycle review reproduced and verified fixes for nested unawaited returns,
translated/swallowed cancellation, timeout identity and special-method descriptor binding.
Ruff lint/format, strict Mypy, Bandit, lock consistency, the executable async example,
existing demo comparison, wheel/sdist build, Twine and wheel-content checks passed.
These local results do not claim that remote CI or provider-network interoperability ran.

## Bounded event-stream framing increment

The original `SSEDecoder` adds actual incremental UTF-8/line/field framing with
explicit ownership, complete immutable messages, control-state/EOF diagnostics
and independent byte, line, data, output-amplification and work budgets. It has no
HTTP client, connection or provider API call. The offline example explicitly
separates SSE messages, JSON fragments and application completion before semantic
approval. See [the exact profile and limits](sse-framing.md).

Final Windows Python 3.14.5 full verification passed 1,727 tests with RuntimeWarning
as an error and 98.33% combined coverage. All 167 new tests also passed on Python
3.12.0; the framing module reached 100%. Independent peer review checked 700
additional seeded streams and exact work totals. Static/lock/package gates, the
offline example and isolated built-wheel smoke passed. HTTP/reconnection,
provider semantics, tool/usage aggregation, full external adapter verification
and other reference repository subsystems remain OPEN. This increment is not a
whole-repository completion declaration.

## Asynchronous final-semantic validation increment

The original `AsyncValidationPipeline` composes the complete synchronous repair/final-check
stage with real awaited reject-only checks over isolated final-field snapshots. It shares
invocation admission with synchronous rules, bounds concurrency without eagerly copying every
field, reports actual callback entries separately from reservations, and settles all owned
coroutines before returning or propagating cancellation. Borrowed tasks/futures are rejected
without being canceled. Read [the precise ownership and deadline contract](async-validation.md).

Final Windows verification passed **1,824 tests** on both Python **3.14.5** (119.17 s) and
**3.12.0** (74.58 s), with no skips and RuntimeWarning promoted to an error. The 3.14 suite
reached **98.36% combined statement/branch coverage**; the existing 95% gate is unchanged.
The 3.12 suite did not collect coverage. All **97** new focused cases passed separately on
both interpreters, including independent reject-rule comparisons, Event-coordinated actual
overlap, shared budgets, post-repair isolation, malformed returned-resource cleanup, borrowed
awaitable ownership, repeated cancellation, genuine cleanup controls, timer ownership and
late synchronous work through final report construction. The new module reached **98.90%**;
two defensive internal invariant guards remain uncovered.

Ruff lint/format, strict Mypy (24 source modules), Bandit, frozen-lock consistency and whitespace
checks passed, along with all seven offline output examples and the existing generated-demo
comparison. No dependency upgrade, package-version change, provider client or key was introduced.

These are local implementation checks, not remote CI or a claim that this repository matches
the complete reference. Asynchronous repair/filter semantics, provider/stream integration of
this separate API, worker isolation, distributed validation and all other open rows remain open.

## Explicit dataclass adaptation increment

The new distinct `DataclassAdapter` constructs real nested stdlib dataclasses through the existing
annotation compiler and schema evaluator. It includes input/output omission semantics, captured
literal defaults, per-call factory results projected and reconstructed without bypassing actual
constructors, pre-callback input/union checks, monotonic work and callback budgets, and final object
projection/revalidation. Tests exercise observable constructor side effects, aliases, late mutation,
ambiguity, exact types, inherited/frozen/slotted fields and fresh-versus-borrowed async resources.
See [the exact supported subset and trust boundary](dataclass-adaptation.md).

Comparison uses the frozen first-party Pydantic
[dataclass contract](https://github.com/pydantic/pydantic/blob/2261ae19e2e09f792f06613360c83fc829238111/docs/concepts/dataclasses.md)
and [type-adapter contract](https://github.com/pydantic/pydantic/blob/2261ae19e2e09f792f06613360c83fc829238111/docs/concepts/type_adapter.md).
This original implementation is not a Pydantic wrapper or a claim to implement all those contracts.
Recursive/generic models, aliases, rich standard types (now narrowed by the next increment), custom serializers, assignment/call
validation, compiled acceleration and complete cross-engine compatibility remain open.

Final Windows / Python **3.14.5** verification passed **1,896 tests**, zero skips, in **76.35 s**
with RuntimeWarning promoted to an error. Combined statement/branch coverage was **98.46%**;
the existing **95%** gate was unchanged. All **72** new focused cases also passed separately on
Python **3.12.0**. The dataclass module reached **99.62%** (one private no-union-match defensive
guard remains uncovered); the shared annotation compiler and schema evaluator reached **100%**.

Independent review checked nested preflight, ambiguity, shared work, direct coroutine ownership
and error privacy. Final admission tests also reject explicit `default_factory=None`, separately
from an absent factory; callback signatures and missing-value sentinels are not conflated.
Ruff lint/format, strict Mypy (25 source modules), Bandit, lock/whitespace checks, all eight offline
output examples, the existing demo comparison, wheel-from-sdist build, Twine, wheel contents and
isolated installed-wheel execution passed. Both distributions include the new module, and the
sdist includes its documentation, example and tests. There are no runtime dependency, project
version, provider or key changes. These are local results, not a claim that hosted CI or broad
Pydantic differential compatibility has run on this uncommitted increment.

## Increment: integrated strict standard-library scalar fields

`DataclassAdapter` now constructs twelve concrete temporal/Decimal/UUID/IP types
through its existing compiled plan, preparation/default machinery and final graph
validation. The JSON-only annotation adapter remains unchanged. Scalar-bearing
unions use bounded pure semantic preflight without trial user constructors;
canonical wire constraints, timezone/scale decisions and schema projection limits
are documented in [the full contract](typed-scalar-fields.md).

Thirteen initial tests failed on the published code because these annotations
were unsupported. A separate hostile-context regression exposed Decimal's
context-sensitive exponent capitalization; the original codec now emits exact
tuple-based canonical strings without ambient context mutation. Independent
250-tuple representation comparisons preserve sign, coefficient and exponent,
including 1000 digits and the admitted exponent extremes. These focused results
do not replace final whole-suite/package/hosted gates.

Coercion policies, richer type families, recursive/generic definitions, aliases,
custom serializers, complete JSON Schema dialects and the other whole-reference
requirements remain open. The
[frozen standard-library reference contract](https://github.com/pydantic/pydantic/blob/2261ae19e2e09f792f06613360c83fc829238111/docs/api/standard_library_types.md)
was checked for public capability scope. This is an independently authored strict
wire subset, not a declaration of broad cross-engine equivalence.

Windows Python **3.14.5** full verification passed **2,060 tests**, zero skips,
in **117.30 s**, with RuntimeWarning and ResourceWarning treated as errors.
Combined coverage was **98.5360%**: 4959/5016 statements and 2041/2088 branches.
All **164** new cases separately passed on Python **3.12.13** (12.05 s).
The scalar module and both affected annotation/dataclass modules reached **100%**
statement/branch coverage in the full run and a separate 310-case focused run.
The existing repository coverage gate remains unchanged. Independent review
checked actual pre-constructor rejection, ambiguous unions, hostile Decimal
contexts, coefficient-scale round trips and typed/default ownership boundaries.

Ruff lint/format (88 files), strict Mypy (26 modules), Bandit, frozen 62-package
lock and whitespace checks passed. All nine offline output examples and the
existing demo comparison passed. A wheel built from the sdist passed strict
Twine and wheel-content checks. Its 27 package files matched source and a fresh
offline isolated installation; eight incremental documentation/test/example
files matched the sdist. The isolated installed package ran the actual typed-scalar
and nested-dataclass examples. No package version, dependency or runtime key
changed. Hosted validation is separate and is not claimed by these local results.
