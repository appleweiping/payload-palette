# API ingress integration

`normalize_json_bytes()` is the framework-neutral boundary for an HTTP body that has already been
read as bytes. It uses the same strict JSON rules as the command line and then applies the normal
payload policy:

```python
from payload_palette import NormalizationPolicy, normalize_json_bytes


def handle_multimodal_request(body: bytes, content_type: str | None):
    if content_type is None or content_type.partition(";")[0].strip().lower() != "application/json":
        raise UnsupportedMediaType("expected application/json")
    manifest = normalize_json_bytes(
        body,
        NormalizationPolicy(max_parts=32, max_total_inline_bytes=32 * 1024 * 1024),
        max_input_bytes=48 * 1024 * 1024,
    )
    return manifest.to_dict()
```

`UnsupportedMediaType` is an application-specific error in this example, not a Payload Palette
type. The adapter intentionally leaves HTTP status mapping to the application. A
`PayloadValidationError` is normally mapped to a 400 response; a request rejected by the server's
body limit is normally mapped to 413.

## Required outer controls

Set the reverse-proxy and application-server body limits to the same value or lower than
`max_input_bytes`. Otherwise the server may buffer an oversized body before Payload Palette sees
it. The HTTP layer also owns:

- authentication, authorization, rate limiting, concurrency limits, and request timeouts;
- `Content-Type`, `Content-Encoding`, and transfer-decoding policy;
- compressed and decompressed body limits;
- request-smuggling and ambiguous-header defenses;
- correlation IDs and redaction of validation errors in logs.

The library owns strict UTF-8/JSON decoding, envelope parsing, inline byte accounting, remote
reference policy, MIME/signature checks, and deterministic manifest construction. It never performs
a fetch. A downstream fetcher remains responsible for DNS resolution, IP checks after resolution,
redirect policy, TLS validation, download limits, container parsing, and sandboxing.

The machine-readable [security policy matrix](../benchmarks/security_policy_matrix.json) records
this ownership split for deployment review.
