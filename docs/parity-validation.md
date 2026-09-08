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
| Schema generation and Python type system | `AnnotationAdapter`, `schema_for_annotation`, `tests/test_annotation_adapter.py` | Partial: strict primitives, list/dict, unions, Literal, TypedDict and explicit Annotated constraints; integer representation distinguished from JSON mathematical semantics. Generic models, forward references, dataclasses, callable validation, aliases and rich standard types remain open. |
| Validator composition and failure actions | `OutputValidator`, `RuleBinding`, `ValidationPipeline`, `tests/test_output_validation.py` | Partial: synchronous exact-path reject/fix/object-filter actions, immediate repair check and final verification. Wildcard dispatch, broader action semantics and interoperable validator serialization remain open. |
| Model invocation and re-asking | `AsyncModelProvider`, `AsyncGenerationRunner`, `tests/test_generation.py` | Partial: provider-neutral complete-output lifecycle, bounded schema/semantic re-asks, explicit usage and offline failure tests. Live provider adapters, provider interoperability, richer field regeneration and generation-quality evaluations remain open. |
| Async and concurrency | Actual asynchronous provider protocol, cooperative deadlines/cancellation, concurrent-run isolation tests | Partial: sequential per-run lifecycle with shared budgets and explicit cancellation behavior. Async semantic validators, bounded parallel field scheduling, isolated workers and distributed execution remain open. |
| Incremental output validation | `IncrementalOutputSession`, `validate_output_chunks`, `tests/test_incremental_output.py` | Partial: finite-state UTF-8/JSON parsing, immutable provisional complete-value events, explicit EOF validation and whole-document differential cases. Incremental semantic rules, async provider streaming, backpressure and reference performance remain open. |
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
