# Streaming ingress

Payload Palette can validate a request stream without retaining the entire byte
body or a large encoded media value.

`normalize_json_bytes` needs the body, its decoded text, and the parsed document
at the same time. For a request carrying one large inline image that is roughly
three copies of the media before any manifest exists. `normalize_stream` reads
the same document from a byte stream and produces the same manifest, with peak
memory following the *structure* of the request instead of the size of its
media.

```python
from payload_palette import NormalizationPolicy, normalize_stream

with open("request.json", "rb") as handle:
    manifest = normalize_stream(handle, NormalizationPolicy())
print(manifest.fingerprint, manifest.inline_bytes)
```

The CLI exposes the same path with `--stream`:

```bash
payload-palette normalize request.json --stream -o manifest.json
```

## Why it is bounded

A manifest entry for inline media records three things: the decoded byte length,
a SHA-256 digest, and the leading `SIGNATURE_PREFIX_BYTES` bytes. None of them
needs the payload afterwards. The decoder therefore folds a long string into
those three values *while its characters are still going past*, and keeps only
the first `LARGE_VALUE_PREFIX_CHARACTERS` of the string itself.

The result is a `LargeValue`: the true character count, the retained prefix, and
either a media summary or the refusal that prevented one. The rest of the
pipeline treats it as a string that happens to have measurements instead of
characters.

Measured on one request holding a single inline PNG, with `tracemalloc`:

| inline media | buffered peak | streamed peak | reduction |
| --- | --- | --- | --- |
| 1 MiB | 3.0 MB | 1.49 MB | 2.0x |
| 8 MiB | 22.6 MB | 1.49 MB | 15.1x |
| 32 MiB | 89.7 MB | 1.49 MB | 60.0x |

The streamed peak is the same number in all three rows, which is the actual
claim: it is set by the read chunk and one Base64 block, not by the payload.
`tests/test_streaming.py` asserts the *slope* rather than a fixed ceiling,
because an absolute bound would only pin the chunk size and would still pass if
the decoder quietly started retaining payloads.

## What a measured value can be used for

The threshold above which a string is measured is deliberately higher than every
limit that would have accepted a string of that length for anything except
inline media:

```
threshold = max(max_text_characters, MAX_REMOTE_URL_CHARACTERS,
                LARGE_VALUE_PREFIX_CHARACTERS) + 1
```

So a value the stream declined to materialize is one the pipeline was going to
refuse anyway, and it is refused for the *same reason* with the *same code*:

- used as a text part, `text_too_long` -- reporting the true length, which the
  stream counted without keeping the characters;
- used as a remote URL, `url_too_long`;
- used as inline media, normalized from the summary the stream already built.

A data URL keeps its header inside the retained prefix, because
`MAX_DATA_URL_HEADER_CHARACTERS` is far below the prefix length. The header goes
through exactly the grammar a buffered header does; only the payload after the
comma stays a measurement.

## Deferred refusals

A long string is summarized before anything knows what it is. Refusing a
malformed Base64 payload at that moment would report it at the JSON path rather
than at the content path, and would refuse a *text* part for a fault that only
matters to media. The refusal therefore travels with the value and is raised by
whoever decides the value is media, against the correct path:

```
$.messages[0].content[1].image_url.url: [invalid_base64] payload is not valid standard Base64
```

## Equivalence, and its one limit

A document accepted by the streaming path is accepted by the buffered path and
yields an identical manifest, fingerprint included. This is checked by
differential testing rather than asserted:

- 20,000 randomized Base64 values, standard and URL-safe, padded and unpadded,
  whitespace-peppered, truncated and corrupted, at chunk sizes from 1 character
  to 64 KiB: identical accept/reject, identical byte length, digest and
  signature prefix, and identical issue codes.
- 6,000 randomized JSON documents including inline media, escapes, surrogate
  pairs, deep nesting and 26 corruption modes, at chunk sizes from 1 byte to
  256 KiB: no difference in outcome.
- 1,500 randomized multimodal requests across three media sizes: no difference
  in the resulting manifest.

The one difference is error *precedence* on a document that breaks more than one
rule at once. Each path reports the first fault it can see, and they see the
document differently: the buffered path decodes the entire body as UTF-8 before
it parses anything, so a body with trailing invalid bytes is always an
`input_encoding` fault to it, while the streaming path reaches an earlier syntax
error first. Both refuse; only the reported code differs. In the 6,000-document
run, restricting the corpus to valid UTF-8 left exactly one such document -- one
truncated in the middle of a surrogate escape, where the two faults occupy the
same position.

Single-fault documents get identical codes from both paths, and that is what the
parametrized cases in `tests/test_streaming.py` pin.

## What streaming does not change

Streaming is a decoding strategy, not a policy. Every size cap, MIME allowlist,
signature check, part-count limit and the remote-URL default-deny apply exactly
as they do to a buffered request. The package still performs no network access,
and `normalize_stream` reads only the stream it is handed. `normalize_path` is
the single function that opens a file, and it opens exactly the path it is
given: no path resolution, and no following of references found inside the
document.

Standard input is read through its binary buffer, because streaming needs bytes
rather than decoded text. A text stream with no buffer is refused rather than
re-encoded, since re-encoding would reintroduce the whole-request copy the
caller asked to avoid.
