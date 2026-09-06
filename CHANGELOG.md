# Changelog

All notable changes are documented here. Versions follow Semantic Versioning.

## [Unreleased]

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

### Changed

- Normalization now folds inline media into its byte length, SHA-256 digest, and the documented
  `SIGNATURE_PREFIX_BYTES` signature prefix as blocks arrive, instead of holding the whole
  decoded payload. Manifests, fingerprints, limits, and error codes are unchanged; peak traced
  memory for a 4 MiB inline image drops from roughly 5x the decoded size to under 0.1x.

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
