"""One fixed child-process comparison protocol. Not an arbitrary code runner."""

from __future__ import annotations

import base64
import re
import sys
from dataclasses import asdict
from typing import Any

from benchmarks.corpus import CorpusCase, _fields, canonical, chunk_schedule
from benchmarks.corpus_replay import _difference, compare_case
from payload_palette.ingress import decode_json_bytes

MAX_REQUEST = 512 * 1024
MAX_RESPONSE = 64 * 1024


def request_bytes(case: CorpusCase, chunks: tuple[int, ...]) -> bytes:
    chunk_schedule(chunks)
    fields = asdict(case)
    fields["raw"] = base64.b64encode(case.raw).decode("ascii")
    raw = canonical({"version": 1, "case": fields, "chunks": chunks})
    if len(raw) > MAX_REQUEST:
        raise ValueError("worker request exceeds byte ceiling")
    return raw


def parse_request(raw: bytes) -> tuple[CorpusCase, tuple[int, ...]]:
    value = decode_json_bytes(raw, max_input_bytes=MAX_REQUEST)
    if (
        type(value) is not dict
        or set(value) != {"version", "case", "chunks"}
        or type(value["version"]) is not int
        or value["version"] != 1
    ):
        raise ValueError("invalid comparison request")
    fields = value["case"]
    if type(fields) is not dict or set(fields) != set(CorpusCase.__dataclass_fields__):
        raise ValueError("invalid case fields")
    if type(fields["raw"]) is not str or len(fields["raw"]) > 349528:
        raise ValueError("invalid case encoding")
    fields["raw"] = base64.b64decode(fields["raw"], validate=True)
    for name in ("parameters", "profile", "precedence"):
        items = fields[name]
        if type(items) is not list or len(items) > 32:
            raise ValueError("invalid bounded metadata list")
        if any(type(item) is not list or len(item) != 2 for item in items):
            raise ValueError("metadata entries must be pairs")
        fields[name] = tuple(tuple(item) for item in items)
    chunks = value["chunks"]
    if type(chunks) is not list or not 1 <= len(chunks) <= 8:
        raise ValueError("invalid chunk schedule")
    return CorpusCase(**fields), chunk_schedule(tuple(chunks))


def _observation(value: Any) -> None:
    if type(value) is not dict:
        raise ValueError("invalid observation object")
    status = value.get("status")
    if status == "accepted":
        _fields(value, {"status", "manifest_sha256", "fingerprint"}, "accepted observation")
        for name in ("manifest_sha256", "fingerprint"):
            pattern = r"sha256:[0-9a-f]{64}" if name == "fingerprint" else r"[0-9a-f]{64}"
            if type(value[name]) is not str or not re.fullmatch(pattern, value[name]):
                raise ValueError("invalid observation digest")
    elif status == "rejected":
        _fields(value, {"status", "issues"}, "rejected observation")
        issues = value["issues"]
        if type(issues) is not list or not issues:
            raise ValueError("missing ordered issues")
        for pair in issues:
            if type(pair) is not list or len(pair) != 2 or any(type(x) is not str for x in pair):
                raise ValueError("invalid code/path pair")
    elif status == "exception":
        _fields(value, {"status", "type"}, "exception observation")
        if type(value["type"]) is not str or not value["type"]:
            raise ValueError("invalid exception type")
    else:
        raise ValueError("invalid observation status")


def parse_response(raw: bytes, case: CorpusCase, chunks: tuple[int, ...]) -> dict[str, Any]:
    """Validate every compact observation before counters consume its fields."""
    chunk_schedule(chunks)
    value = _fields(
        decode_json_bytes(raw, max_input_bytes=MAX_RESPONSE),
        {"case_id", "descriptor", "outcome", "reason", "buffered", "streams"},
        "comparison response",
    )
    if value["case_id"] != case.case_id or canonical(value["descriptor"]) != canonical(
        case.descriptor()
    ):
        raise ValueError("response case identity mismatch")
    _observation(value["buffered"])
    streams = value["streams"]
    if type(streams) is not list or len(streams) != len(chunks):
        raise ValueError("response schedule length mismatch")
    reasons = []
    for item, chunk in zip(streams, chunks, strict=True):
        _fields(item, {"chunk", "observation", "difference", "axes"}, "stream response")
        if type(item["chunk"]) is not int or item["chunk"] != chunk:
            raise ValueError("response schedule mismatch")
        _observation(item["observation"])
        # The child compared full manifest bytes. Independently bind its summary
        # to the retained digests/diagnostics rather than trusting its labels.
        buffered = {**value["buffered"], "manifest_bytes": value["buffered"].get("manifest_sha256")}
        streamed = {
            **item["observation"],
            "manifest_bytes": item["observation"].get("manifest_sha256"),
        }
        reason = _difference(case, buffered, streamed)
        axes = []
        if buffered["status"] == streamed["status"] == "accepted":
            axes = [
                name
                for name in ("manifest_bytes", "fingerprint")
                if buffered[name] != streamed[name]
            ]
        if item["difference"] != reason or item["axes"] != axes:
            raise ValueError("comparison labels contradict their observations")
        if reason is not None:
            reasons.append(reason)
    unexpected = [reason for reason in reasons if reason != "expected_precedence"]
    outcome = "mismatch" if unexpected else "expected_precedence" if reasons else "match"
    reason = unexpected[0] if unexpected else reasons[0] if reasons else None
    if (value["outcome"], value["reason"]) != (outcome, reason):
        raise ValueError("response summary contradicts its comparisons")
    return value


def main() -> None:
    result: dict[str, Any]
    try:
        case, chunks = parse_request(sys.stdin.buffer.read(MAX_REQUEST + 1))
        result = compare_case(case, chunks)
    except BaseException as error:
        # This process is the tested child, not the user's controlling process.
        # Its controls/fatal failures must be visible as observations to the parent.
        result = {"worker_error": type(error).__module__ + "." + type(error).__qualname__}
    wire = canonical(result)
    if len(wire) > MAX_RESPONSE:
        wire = canonical({"worker_error": "response_limit"})
    sys.stdout.buffer.write(wire)


if __name__ == "__main__":
    main()
