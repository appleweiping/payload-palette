# Changelog

All notable changes are documented here. Versions follow Semantic Versioning.

## [Unreleased]

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
