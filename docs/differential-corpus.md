# Offline differential corpus

The research tool in `benchmarks/corpus*.py` generates bounded, reproducible
multimodal input cases and compares the actual buffered and streaming entry
points. It is original test tooling, not a new parser, vendor SDK, network
service, or performance benchmark. It ships with the repository/source
distribution; it is not included in the runtime wheel.

The [parity audit](parity-validation.md#offline-differential-corpus-and-counterexamples)
records the full local acceptance results and their limits, including preserved
development failures. Matching observations do not prove complete parser correctness.

## Run and replay

Use the locked development environment from the repository root:

```bash
python -m benchmarks.corpus_cli plan --output corpus-plan.json
python -m benchmarks.corpus_cli run --smoke --output corpus-smoke.json
python -m benchmarks.corpus_cli run --output corpus-full.json
python -m benchmarks.corpus_cli replay --case-id CASE_SHA256 --output replay.json
python -m benchmarks.corpus_cli minimize --case-id CASE_SHA256 --attempts 64 --output reduction.json
```

Output names must be new; existing reports are never overwritten. `run` and
`replay` return 0 for matching/explicitly permitted precedence observations, 1
for any mismatch, crash, timeout or final source-stability failure, and 2 for
invalid configuration or an output error. A report with nonzero outcome is
useful evidence, not something to delete or filter out. Each row includes a
replay argument list. Preserve the configuration, rule hash and implementation
provenance when comparing reports from different machines or revisions.

The default seed is `20260908`, with four deterministic repetitions and chunk
sizes `(1, 7, 64, 4096)`. Repetition data comes from a fixed SHA-256 input recipe,
not Python's randomized `hash()` or unspecified PRNG state. The case order is
declared envelope/recipe/repetition order followed by retained independent seed
order. Smoke selection is `int(case_id[:8], 16) % 16 == 0`; it does not examine
whether a case passes. Selection is an offline stable partition, not a claim to
cover every family on its own.

## What is compared

Every case has an explicit comparison class fixed before its results:

| Class | Required observation |
| --- | --- |
| `valid` | Both paths accept; exact canonical manifest bytes and fingerprints match |
| `single` | Both reject; ordered stable issue codes and source paths match |
| `invalid` | Both reject, without claiming an isolated fault; unknown diagnostic differences remain mismatches |
| `multiple` | Both reject; only the explicitly recorded lexical precedence pair may differ |
| `equivalent` | Acceptance/rejection agrees; accepted manifests or rejected diagnostics agree |

`equivalent` does **not** replace an independent must-reject oracle. In
particular, independent `n_` JSON tokens use `invalid`: a shared bug that accepts
both sides is a failure, not a match. Independent implementation-defined `i_`
tokens use `equivalent`; this does not claim full RFC compliance for bounded
integer, finite-number or Unicode handling.

For the sole permitted precedence example, malformed syntax comes before a
later invalid UTF-8 byte. Both paths must reject. Buffered `input_encoding`
versus streamed `invalid_json` is classified only for a case explicitly marked
`multiple`, only for that ordered code pair and only for equal source paths.
It need not occur for every chunk size. Other differences are retained.

All chunk schedules run even after the first discrepancy. The tool compares
the full canonical accepted bytes before retaining their digest in the compact
report. It keeps ordered code/path records for rejections. Unexpected exception
types are recorded without formatting their messages, invoking custom `str`,
or exposing arbitrary exception arguments. A deliberate injected differential
defect, common-mode acceptance of invalid input and changed discrepancy axes
have regression tests.
The parent independently checks response shape, exact descriptor/schedule identity
and that the retained digests and diagnostics agree with every difference label;
a child cannot claim `match` for contradictory observations.

## Original recipes and independent data provenance

Original MIT-licensed controls describe synthetic text and recognizable media
prefixes. They are not customer/model traffic or complete decodable media
containers. Closed mutation rules cover JSON syntax, UTF-8, duplicate keys,
numbers, nesting, Unicode, envelope shape, competing recognized fields, Base64,
data-URL headers, MIME/signature checks, remote-reference policy and small exact
resource boundaries. Longer inline controls actually cross the streaming
measurement threshold. No remote reference is fetched or resolved.

Capabilities remain envelope-specific. The existing Gemini contract uses
`inline_data`/`file_data`, not automatic camelCase SDK translation. Ollama has
no explicit MIME or remote-image-reference equivalent in the supported contract;
those grammar families are exercised where they exist, not misrepresented as
supported Ollama operations. Data-URL headers are tested through the default
envelope that actually interprets them.

Independent test **data**, not implementation source, is retained from
[JSONTestSuite](https://github.com/nst/JSONTestSuite/tree/1ef36fa01286573e846ac449e8683f8833c5b26a),
at immutable commit `1ef36fa01286573e846ac449e8683f8833c5b26a`. Before replay,
we selected the first eight lexicographically ordered `test_parsing` filenames
in each `y_`, `n_`, `i_` group with raw size at most 256 KiB. All other files
are excluded by that fixed rule, not by their outcomes. The 24 retained inputs
are small tokens; they are not a representative sample of every upstream test.

`benchmarks/corpus_data/json_testsuite.json` records the official source URL,
commit, each original path, git blob ID, acquired raw-file SHA-256, raw length,
and exact lossless Base64 storage. Its only replay transformation embeds that
token in an ignored metadata field beside an original valid envelope control.
Original upstream classifications are retained separately. No upstream parser,
wrapper, runner or mutation implementation is copied.

The full applicable MIT notice is in `LICENSE.JSONTestSuite`; its SHA-256 is
`8bd0e0578be788c617ea01d18b2a8146e3746ae50bddadc65a5f9d3aad08ad49`.
The corpus loader checks every retained seed and notice before yielding cases.
These provenance/integrity records are not publisher authentication. Source
acquisition is not performed by the runner, installed package or ordinary tests.

## Admission and identity

The generator, comparator, child protocol and CLI source hashes, retained
data/license bytes, exact configuration and ordered derived descriptors all
contribute to the effective corpus root. Each descriptor records its seed and
source version, mutation parameters, comparison class, policy overrides,
raw-input length/digest and stable case ID. A changed rule or policy/configuration
cannot silently keep the same corpus root.

The whole declared run is preflighted before calling either normalizer. Cases
are expanded one at a time; only bounded metadata/results are retained, never
the whole expanded raw corpus. Programmatic `CorpusConfig` limits can be reduced
but cannot exceed these hard ceilings:

| Bound | Hard ceiling |
| --- | --- |
| Retained encoded corpus artifact | 20 MiB |
| One raw seed/derived case | 256 KiB |
| Cases | 10,000 |
| Aggregate generated raw input | 256 MiB |
| Aggregate bytes across all buffered/streamed executions | 256 MiB |
| Distinct chunk schedules per case | 8 |
| Total path executions | 100,000 |
| One worker request / response | 512 KiB / 64 KiB |
| Serialized final report | 64 MiB |

These bounds do not claim a Python allocator/RSS sandbox. In particular, a
bounded JSON artifact is parsed before its fixed field/seed cardinality checks;
application inputs themselves still use the existing bounded ingress contract.
The tool never dynamically imports, evaluates or executes strings from seeds,
case metadata or checkpoint documents.

## Watchdog, reports and shrinking

The CLI gives each case to a fresh owned Python child running one fixed module.
Its deadline covers startup and comparison; timeout kills and settles that child
through the subprocess API, then records the case and continues. No arbitrary
command or user callback is accepted by this process protocol. The fixed child
caps its response before writing. Python, the OS, local interpreter environment
and native subprocess cleanup are trusted; this is not an adversarial child
sandbox or a guarantee about callback-created descendants. Process creation and
OS-level termination themselves are not forcibly interruptible Python steps.
On Windows venvs, the supervisor launches the base interpreter with CPython's
venv-launcher environment, so the owned process is the interpreter rather than
a redirector whose descendant would retain its output pipes. An actual sleeping
interpreter timeout is tested separately from mocked watchdog outcomes.

Reports separate ordered semantic results from declared environment information:
package version, available Git commit/dirty status, normalizer source digest,
Python implementation/version, OS/machine and watchdog setting. Missing Git
metadata is explicit, not invented. Counts retain zero-result categories. Source
stability failures at the final audit invalidate the run without dropping its
already-observed rows. A generator failure after admission also retains completed
rows, a failure classification and exception type; ordinary failure messages are
not serialized. Matching semantic outputs do not demonstrate speed, real
traffic prevalence, media validity or security completeness. Timeout observations
can change with the declared runtime and must not be treated as deterministic
performance thresholds.

Shrinking is deterministic contiguous deletion. It preserves the original bytes
and records **every** attempted deletion; accepted reductions retain exact bytes
and a digest chain. The predicate retains outcome/reason, exception type,
ordered diagnostics, schedule and whether accepted-manifest bytes, fingerprint,
or both differed. It does not promise the same internal root cause or a globally
minimal counterexample. Byte work includes the baseline; `max_attempts` counts
deletion trials, so total replays are trials plus one. The bounds are 512 trials
and 32 MiB of processed bytes. CLI shrink trials use the same child watchdog.
The report retains readable baseline and trial observations, not just signature
hashes, plus outcome counts and complete plan/configuration/environment evidence.
Any baseline/trial crash or timeout makes the CLI return 1. An unavailable
baseline is recorded with zero deletion trials and `baseline_failed: true`; it
is not treated as a successful reduction. A normally matching baseline is not
a counterexample and is rejected as inapplicable configuration (exit 2).
Confirmed implementation defects must be retained as permanent regressions
before changing the parser; a matching run is not required to make this tool useful.
