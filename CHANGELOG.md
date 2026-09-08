# Changelog

All notable changes are documented here. Versions follow Semantic Versioning.

## [Unreleased]

### Added

- Added bounded offline incremental SSE framing, explicit control-state/EOF
  diagnostics and an executable framing-to-JSON application example.
- Added backpressured asynchronous output consumption, cooperative deadlines and
  explicit awaitable iterator cleanup before final semantic validation.
- Added incremental strict UTF-8/JSON output parsing with bounded work, immutable
  provisional syntax events, final-only semantic validation and explicit iterator ownership.
- Added bounded runtime schema interchange, union/nullability and typed-map validation, with
  explicit strict-Python versus mathematical-JSON integer semantics and faithful Payload exports.
- Added trusted annotation adapters for primitives, containers, unions, Literal, TypedDict and
  configured constraints, plus isolated validation and exact-byte-bounded JSON serialization.
- Added a bounded strict nested JSON schema API for complete structured model output.
- Added synchronous validator bindings with structured outcomes, reject/fix/filter actions,
  callback and repair validation, final rule verification, and isolated output snapshots.
- Added a provider-independent executable model-output validation example and an explicit
  reference repository gap assessment. Full reference repository parity remains open.
- Added an actual asynchronous model-provider protocol and bounded generate/validate/re-ask lifecycle
  with cooperative deadlines, propagated cancellation, shared usage/response/validator budgets,
  privacy-minimized attempt histories, and deterministic offline adapter examples and tests.

## [0.5.0] - 2026-09-07

### Added

- Added deterministic manifest comparison diagnostics for cache and adapter compatibility checks.
- Added redacted audit receipts that retain identity and sizing facts without text or URL secrets.

## [0.4.0] - 2026-09-07

### Added

- Added bounded batch normalization for offline dataset and queue preflight
  workflows. Records are normalized independently, structured failures are
  retained, and a canonical input SHA-256 is emitted.
- Added a JSON Schema descriptor for manifest interchange and a JSONL loader
  that reuses the strict ingress parser. No remote media is fetched and no
  binary content is retained in batch results.

## [0.3.0] - 2026-09-07

### Added

- Opt-in envelope adapters for the Anthropic Messages, Google Gemini `generateContent`,
  and Ollama request shapes, selected with `NormalizationPolicy(envelope=...)` or
  `--envelope`. A vendor shape is never auto-detected, because guessing would weaken the
  `ambiguous_envelope` rejection the default contract relies on. An adapter translates
  shape only: part order, size caps, MIME policy, signature checks, and the remote-URL
  default-deny all apply unchanged.
- Opt-in RFC 4648 section 5 URL-safe Base64 for inline media, through
  `NormalizationPolicy(allow_url_safe_base64=True)`, `decode_base64(..., allow_url_safe=True)`,
  and the `--allow-url-safe-base64` CLI flag. Decoding stays standard-only by default and the
  opt-in changes no length, padding, MIME, signature, or size rule.
- Stable `url_safe_base64_disabled` and `mixed_base64_alphabet` error codes. A payload that
  contains characters from both alphabets is rejected whether or not the opt-in is enabled.
- A bounded streaming Base64 decoder. `decode_base64(..., sink=...)` decodes in
  `BASE64_BLOCK_CHARACTERS` blocks and hands each decoded block to the sink without retaining
  the payload, enforcing every existing bound against the running decoded total.
- Incremental JSON decoding that avoids buffering the complete raw JSON request body.
  `normalize_stream`, `normalize_path`, `decode_stream`, and the `--stream` CLI flag read a byte
  stream in bounded chunks and produce the identical manifest. Ordinary structure and accepted
  text are still materialized; long media strings are reduced to bounded summaries. The nesting
  ceiling is now enforced while parsing rather than by a separate character pass over the whole
  document, and duplicate keys, non-standard numeric constants, non-finite floats, oversized
  integers, isolated surrogates, and the input byte limit are all applied as the characters arrive.
- `LargeValue`: a string past the streaming threshold, kept as its true character count, its leading
  `LARGE_VALUE_PREFIX_CHARACTERS` characters, and the Base64 summary folded in while the value
  streamed past. The threshold sits above `max_text_characters` and `MAX_REMOTE_URL_CHARACTERS`, so
  such a value is refused as text or as a URL for the reason it would have been refused anyway, with
  `text_too_long` reporting the true length. A refusal discovered while summarizing travels with the
  value and is raised against the content path by whoever decides the value is media.
- `Base64Digester`, the streaming form of the Base64 contract, and `StreamStatistics`, which reports
  what a parse actually retained.

### Changed

- CI and tagged releases now consume the frozen dependency lock with pinned automation actions;
  publishing requires successful cross-platform tests and CodeQL, uses reproducible archive
  timestamps, emits checksums and provenance, and refuses to replace an existing release asset.
- Normalization now folds inline media into its byte length, SHA-256 digest, and the documented
  `SIGNATURE_PREFIX_BYTES` signature prefix as blocks arrive, instead of holding the whole
  decoded payload. Manifests, fingerprints, limits, and error codes are unchanged; peak traced
  memory for a 4 MiB inline image drops from roughly 5x the decoded size to under 0.1x.
- Peak memory for a streamed request no longer follows its media size. Measured with `tracemalloc`
  on one request holding a single inline PNG, the buffered path peaks at roughly twice the request
  while the streamed path peaks at the same 1.5 MB whether the image is 1 MiB, 8 MiB, or 32 MiB --
  a 60x reduction at 32 MiB, and growing with the payload.
- `PartSpec.value` is now `str | LargeValue`. A part built by hand from a string is unaffected.
- Streaming is opt-in rather than the default, because a document that breaks more than one rule at
  once can be reported under different issue codes by the two paths: each names the first fault it
  can see, and the buffered path validates the encoding of the whole body before parsing anything.
  Accept/reject and the resulting manifest are identical, which is checked by differential testing
  over 20,000 Base64 values, 6,000 JSON documents, and 1,500 multimodal requests rather than
  asserted. `docs/streaming.md` records the method, the measurements, and the one worked exception.

## [0.2.0] - 2026-09-01

### Added

- A public, bounded raw-bytes ingress adapter shared with the CLI's strict JSON contract.
- A machine-readable security responsibility matrix and API integration guidance.
- An environment-qualified synthetic throughput/resource benchmark and benchmark tests.
- Version compatibility, research limitations, and contribution governance templates.
- Stable falsey-policy, released-memoryview, benchmark tracing, bounded protocol, atomic-output,
  and reference-result validation semantics.

### Planned

- Optional adapters for additional request envelopes.
- Streaming decoders for applications with larger inline-media budgets.

## [0.1.0] - 2026-08-31

### Added

- Ordered parsing for text, image, audio, and video content parts.
- Strict padded and unpadded Base64 decoding and base64 data URL support.
- Default-deny remote URL policy with exact and wildcard host allowlists.
- MIME, per-part, total-inline-size, and known-signature validation.
- SHA-256 fingerprints and deterministic binary-free manifests.
- Structured errors with stable codes and source paths.
- `validate` and `normalize` CLI commands.
- Reproducible example manifest and SVG visualization.

### Hardened

- JSON input now rejects duplicate keys, non-standard/non-finite numbers, oversized integers,
  excessive nesting, invalid UTF-8, and isolated Unicode surrogates.
- CLI input, policy values, flattened parts, and structural errors now have explicit hard bounds.
- URL validation rejects parser-differential control characters, malformed hosts and escapes, and
  ambiguous legacy IPv4 forms before allowlist matching, including forms revealed only after IDNA.
- Remote URLs now have a pre-parse hard limit, linear dot-segment removal, version-stable special-IP
  classification, and rejection for IPv6 zones and multiple trailing host dots.
- Data URL headers have a fixed pre-parse limit and bounded grammar; MIME policy and declaration
  conflicts are rejected before Base64 decoding.
- Text parts obey the same MIME allowlist contract as other part kinds.
- Public model integers stay serializable at CPython's minimum configurable conversion limit.
- File output uses same-directory atomic replacement, and stdout failures use structured errors.
- Normalization errors retain the precise text, Base64, data URL, or remote URL value path.
- Remote fingerprints use a documented RFC 3986 canonicalization subset.
- Normalized models now deeply freeze part collections and attribute mappings.
- Base64 validation rejects partial/excess padding and non-zero padding bits.
- JSON output explicitly forbids `NaN` and infinity, while alias conflicts fail as ambiguous input.
- CLI JSON rejects isolated surrogates even in ignored fields and escapes output for narrow terminal
  encodings; QuickTime and audio-only WebM container signatures are handled consistently.
- CI covers Python 3.14 and Windows and verifies demo artifacts without platform-specific `diff`.
