"""Exact differential observations and bounded, evidence-preserving shrinking."""

from __future__ import annotations

import base64
import io
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from benchmarks.corpus import CorpusCase, canonical, chunk_schedule, digest, integer
from payload_palette import (
    Manifest,
    NormalizationPolicy,
    RemoteURLPolicy,
    normalize_json_bytes,
    normalize_stream,
)
from payload_palette.errors import PayloadValidationError


class ChunkStream(io.BytesIO):
    def __init__(self, raw: bytes, chunk: int) -> None:
        super().__init__(raw)
        self.chunk = integer(chunk, "chunk", 1, 262_144)

    def read(self, size: int | None = -1) -> bytes:
        return super().read(self.chunk if size is None or size < 0 else min(size, self.chunk))


def _policy(case: CorpusCase) -> NormalizationPolicy:
    profile = dict(case.profile)
    cap = profile.get("part_bytes", 65536)
    return NormalizationPolicy(
        envelope=case.envelope,  # type: ignore[arg-type]
        max_parts=profile.get("parts", 16),
        max_text_characters=profile.get("text", 128),
        max_total_inline_bytes=profile.get("total_bytes", 131072),
        max_bytes_by_kind={kind: cap for kind in ("text", "image", "audio", "video")},
        remote=RemoteURLPolicy(allowed_hosts=("media.example.org",)),
        allow_url_safe_base64=bool(profile.get("urlsafe", 0)),
    )


def _observe(function: Callable[..., object], value: object, case: CorpusCase) -> dict[str, Any]:
    try:
        result = function(
            value, _policy(case), max_input_bytes=dict(case.profile).get("input_bytes", 262144)
        )
        if not isinstance(result, Manifest):
            raise TypeError("normalizer returned a non-Manifest value")
        wire = canonical(result.to_dict())
        return {
            "status": "accepted",
            "manifest_bytes": wire,
            "fingerprint": result.fingerprint,
            "manifest_sha256": digest(wire),
        }
    except PayloadValidationError as error:
        return {
            "status": "rejected",
            "issues": [[issue.code, issue.path] for issue in error.issues],
        }
    except Exception as error:
        return {
            "status": "exception",
            "type": type(error).__module__ + "." + type(error).__qualname__,
        }


def _difference(case: CorpusCase, buffered: dict[str, Any], streamed: dict[str, Any]) -> str | None:
    if buffered["status"] == "exception" or streamed["status"] == "exception":
        return "implementation_exception"
    if buffered["status"] != streamed["status"]:
        return "accept_reject"
    accepted = buffered["status"] == "accepted"
    if case.comparison == "valid" and not accepted:
        return "valid_control_rejected"
    if case.comparison in ("single", "multiple", "invalid") and accepted:
        return "malformed_control_accepted"
    if accepted:
        if (
            buffered["manifest_bytes"] != streamed["manifest_bytes"]
            or buffered["fingerprint"] != streamed["fingerprint"]
        ):
            return "manifest_or_fingerprint"
    elif buffered["issues"] != streamed["issues"]:
        if (
            case.comparison == "multiple"
            and len(buffered["issues"]) == len(streamed["issues"]) == 1
            and buffered["issues"][0][1] == streamed["issues"][0][1]
            and tuple((buffered["issues"][0][0], streamed["issues"][0][0])) in case.precedence
        ):
            return "expected_precedence"
        return "diagnostics"
    return None


def compare_case(
    case: CorpusCase,
    chunks: tuple[int, ...],
    *,
    buffered: Callable[..., object] = normalize_json_bytes,
    streamed: Callable[..., object] = normalize_stream,
) -> dict[str, Any]:
    """Compare actual bytes before discarding manifests from the compact report.

    Injected callables are trusted test hooks. Controls propagate in-process;
    the separate CLI worker reports controls inside the tested implementation.
    """
    chunk_schedule(chunks)
    baseline = _observe(buffered, case.raw, case)
    observations: list[dict[str, Any]] = []
    reasons: list[str] = []
    for chunk in chunks:
        with ChunkStream(case.raw, chunk) as stream:
            observation = _observe(streamed, stream, case)
        reason = _difference(case, baseline, observation)
        if reason is not None:
            reasons.append(reason)
        axes = []
        if baseline["status"] == observation["status"] == "accepted":
            axes = [
                name
                for name in ("manifest_bytes", "fingerprint")
                if baseline[name] != observation[name]
            ]
        observations.append(
            {"chunk": chunk, "observation": observation, "difference": reason, "axes": axes}
        )
    unexpected = [reason for reason in reasons if reason != "expected_precedence"]
    baseline.pop("manifest_bytes", None)
    for item in observations:
        item["observation"].pop("manifest_bytes", None)
    return {
        "case_id": case.case_id,
        "descriptor": case.descriptor(),
        "outcome": "mismatch" if unexpected else "expected_precedence" if reasons else "match",
        "reason": unexpected[0] if unexpected else reasons[0] if reasons else None,
        "buffered": baseline,
        "streams": observations,
    }


def _signature(result: dict[str, Any]) -> bytes:
    """Do not shrink to an unrelated fault that merely remains a mismatch."""

    def key(observation: dict[str, Any]) -> list[Any]:
        return [observation.get("status"), observation.get("type"), observation.get("issues")]

    return canonical(
        [
            result["outcome"],
            result["reason"],
            key(result.get("buffered", {})),
            [
                [item["chunk"], item["difference"], item.get("axes", []), key(item["observation"])]
                for item in result.get("streams", [])
            ],
        ]
    )


def minimize_case(
    case: CorpusCase,
    chunks: tuple[int, ...],
    *,
    max_attempts: int = 64,
    max_processed_bytes: int = 32 * 1024 * 1024,
    buffered: Callable[..., object] = normalize_json_bytes,
    streamed: Callable[..., object] = normalize_stream,
    observer: Callable[[CorpusCase], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Deterministic contiguous deletion, not a proof of globally minimal input.

    Every attempted deletion is recorded; accepted states carry exact bytes.
    Original data is retained, never overwritten. Byte work includes the baseline;
    max_attempts bounds deletion trials, so total replays are attempts plus one.
    """
    chunk_schedule(chunks)
    integer(max_attempts, "max_attempts", 1, 512)
    integer(max_processed_bytes, "max_processed_bytes", 1, 32 * 1024 * 1024)
    work = len(case.raw) * (1 + len(chunks))
    if work > max_processed_bytes:
        raise ValueError("baseline exceeds minimization byte budget")

    def observe(candidate: CorpusCase) -> dict[str, Any]:
        if observer is not None:
            return observer(candidate)
        return compare_case(candidate, chunks, buffered=buffered, streamed=streamed)

    baseline = observe(case)
    if baseline["outcome"] not in ("mismatch", "crash", "timeout"):
        raise ValueError("only an actual mismatch can be minimized")
    baseline_failed = baseline["outcome"] != "mismatch"
    counts = dict.fromkeys(("match", "expected_precedence", "mismatch", "timeout", "crash"), 0)
    counts[baseline["outcome"]] += 1

    def evidence(observed: dict[str, Any]) -> dict[str, Any]:
        return {
            name: observed[name]
            for name in (
                "outcome",
                "reason",
                "buffered",
                "streams",
                "type",
                "returncode",
                "stderr_sha256",
                "response_sha256",
            )
            if name in observed
        }

    signature = _signature(baseline)
    current = case.raw
    attempts: list[dict[str, Any]] = []
    width = max(1, len(current) // 2)
    exhausted = False
    while not baseline_failed and current and len(attempts) < max_attempts:
        changed = False
        start = 0
        while start < len(current) and len(attempts) < max_attempts:
            stop = min(len(current), start + width)
            candidate = current[:start] + current[stop:]
            cost = len(candidate) * (1 + len(chunks))
            if work + cost > max_processed_bytes:
                exhausted = True
                break
            work += cost
            observed = observe(replace(case, raw=candidate))
            counts[observed["outcome"]] += 1
            accepted = _signature(observed) == signature
            step: dict[str, Any] = {
                "before_sha256": digest(current),
                "delete": [start, stop],
                "candidate_sha256": digest(candidate),
                "accepted": accepted,
                "observation_signature": digest(_signature(observed)),
                "observation": evidence(observed),
            }
            if accepted:
                current = candidate
                step["base64"] = base64.b64encode(current).decode("ascii")
                changed = True
            else:
                start += width
            attempts.append(step)
        if exhausted:
            break
        if not changed:
            if width == 1:
                break
            width = max(1, width // 2)
    return {
        "version": 1,
        "case_id": case.case_id,
        "descriptor": case.descriptor(),
        "chunks": list(chunks),
        "max_attempts": max_attempts,
        "max_processed_bytes": max_processed_bytes,
        "baseline": evidence(baseline),
        "baseline_failed": baseline_failed,
        "counts": counts,
        "original_sha256": case.raw_sha256,
        "original_base64": base64.b64encode(case.raw).decode("ascii"),
        "signature": digest(signature),
        "attempts": len(attempts),
        "total_replays": len(attempts) + 1,
        "processed_bytes": work,
        "byte_budget_exhausted": exhausted,
        "steps": attempts,
        "final_sha256": digest(current),
        "final_bytes": len(current),
        "final_base64": base64.b64encode(current).decode("ascii"),
        "global_minimum_claimed": False,
    }
