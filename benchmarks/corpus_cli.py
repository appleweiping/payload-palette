"""Offline case planning/replay with an owned per-case process watchdog."""

from __future__ import annotations

import argparse
import os
import platform
import shutil

# Fixed local interpreter and read-only Git; no shell or payload argv.
import subprocess  # nosec B404
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import payload_palette
from benchmarks.corpus import (
    FAMILIES,
    CorpusCase,
    CorpusConfig,
    canonical,
    corpus_plan,
    digest,
    iter_cases,
)
from benchmarks.corpus_replay import minimize_case
from benchmarks.corpus_worker import MAX_RESPONSE, parse_response, request_bytes
from payload_palette import ENVELOPE_NAMES, __version__

ROOT = Path(__file__).resolve().parent.parent
OUTCOMES = ("match", "expected_precedence", "mismatch", "timeout", "crash")
KNOWN_CODES = (
    "invalid_json",
    "input_encoding",
    "duplicate_json_key",
    "nonstandard_json_number",
    "json_number_range",
    "json_too_deep",
    "invalid_unicode",
    "invalid_base64",
    "input_too_large",
    "text_too_long",
    "part_too_large",
    "too_many_parts",
    "url_scheme",
    "url_host",
    "invalid_url",
    "signature_mismatch",
    "mime_not_allowed",
)


def guarded_compare(case: CorpusCase, chunks: tuple[int, ...], timeout: float) -> dict[str, Any]:
    if type(timeout) not in (int, float) or not 0 < timeout <= 120:
        raise ValueError("per-case timeout must be finite seconds in (0,120]")
    row = {"case_id": case.case_id, "descriptor": case.descriptor()}
    request = request_bytes(case, chunks)
    command, environment = _worker_command()
    try:
        # The fixed worker receives case bytes on stdin, never as code or argv.
        process = subprocess.run(  # nosec B603
            command,
            cwd=ROOT,
            env=environment,
            input=request,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {**row, "outcome": "timeout", "reason": "worker_deadline"}
    except OSError as error:
        return {**row, "outcome": "crash", "reason": "worker_start", "type": type(error).__name__}
    if process.returncode != 0 or process.stderr or len(process.stdout) > MAX_RESPONSE:
        return {
            **row,
            "outcome": "crash",
            "reason": "worker_exit_or_output",
            "returncode": process.returncode,
            "stderr_sha256": digest(process.stderr),
        }
    try:
        result = parse_response(process.stdout, case, chunks)
    except (ValueError, TypeError):
        return {
            **row,
            "outcome": "crash",
            "reason": "worker_protocol",
            "response_sha256": digest(process.stdout),
        }
    return result


def _worker_command() -> tuple[list[str], dict[str, str] | None]:
    """Own the actual interpreter, not the Windows venv redirector process.

    CPython's venv redirector otherwise creates a descendant holding stdout;
    killing only the redirector cannot settle a hung interpreter. The launcher
    environment preserves the same venv while launching its base executable.
    """
    executable = sys.executable
    environment = None
    if sys.platform == "win32":
        base = getattr(sys, "_base_executable", None)
        if type(base) is not str or not os.path.isabs(base):
            raise ValueError("Windows watchdog requires a known base interpreter")
        if os.path.normcase(base) != os.path.normcase(executable):
            environment = os.environ.copy()
            environment["__PYVENV_LAUNCHER__"] = executable
            executable = base
    return [executable, "-m", "benchmarks.corpus_worker"], environment


def _provenance() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        executable = shutil.which("git")
        if executable is None:
            return None
        try:
            # Only the two fixed read-only calls below can reach this helper.
            result = subprocess.run(  # nosec B603
                [executable, *args],
                cwd=ROOT,
                capture_output=True,
                check=False,
                timeout=5,
                text=True,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    status = git("status", "--porcelain", "--untracked-files=normal")
    source_hashes: list[list[str]] = []
    for source in sorted(Path(payload_palette.__file__).parent.glob("*.py")):
        with source.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("normalizer source exceeds audit bound")
        source_hashes.append([source.name, digest(raw)])
    return {
        "package_version": __version__,
        "repository_commit": git("rev-parse", "HEAD"),
        "working_tree_dirty": None if status is None else bool(status),
        "normalizer_sources_sha256": digest(canonical(source_hashes)),
        "runtime": {
            "implementation": platform.python_implementation(),
            "python": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
        },
    }


def run_corpus(
    config: CorpusConfig, *, timeout: float = 10.0, case_id: str | None = None
) -> dict[str, Any]:
    plan = corpus_plan(config)
    provenance = _provenance()
    counts: dict[str, dict[str, int]] = {
        "envelope": dict.fromkeys(ENVELOPE_NAMES, 0),
        "family": dict.fromkeys(FAMILIES, 0),
        "outcome": dict.fromkeys(OUTCOMES, 0),
        "issue_code": dict.fromkeys(KNOWN_CODES, 0),
    }
    rows: list[dict[str, Any]] = []
    stability_errors = []
    iteration_failure = None
    cases = iter_cases(config)
    while True:
        try:
            case = next(cases)
        except StopIteration:
            break
        except Exception as error:
            stability_errors.append("corpus_iteration_failed")
            iteration_failure = type(error).__module__ + "." + type(error).__qualname__
            break
        if case_id is not None and case_id != case.case_id:
            continue
        row = guarded_compare(case, config.chunks, timeout)
        counts["envelope"][case.envelope] += 1
        counts["family"][case.family] += 1
        counts["outcome"][row["outcome"]] += 1
        observations = [row.get("buffered", {})] + [
            item["observation"] for item in row.get("streams", [])
        ]
        for observation in observations:
            for code, _path in observation.get("issues", []):
                counts["issue_code"][code] = counts["issue_code"].get(code, 0) + 1
        row["replay"] = [
            sys.executable,
            "-m",
            "benchmarks.corpus_cli",
            "replay",
            "--case-id",
            case.case_id,
            "--seed",
            str(config.seed),
            "--repeats",
            str(config.repeats),
            "--chunks",
            ",".join(str(chunk) for chunk in config.chunks),
            "--timeout",
            str(timeout),
            "--output",
            "NEW-REPORT.json",
        ]
        rows.append(row)
    if case_id is not None and not rows and not stability_errors:
        raise ValueError("case ID is not present in this admitted corpus/configuration")
    try:
        if corpus_plan(config) != plan:
            stability_errors.append("corpus_changed")
        final = _provenance()
        if final["normalizer_sources_sha256"] != provenance["normalizer_sources_sha256"]:
            stability_errors.append("normalizer_changed")
    except (OSError, ValueError):
        stability_errors.append("final_source_audit_failed")
    semantic = {
        "version": 1,
        "plan": asdict(plan),
        "configuration": asdict(config),
        "selection": case_id,
        "counts": counts,
        "cases": rows,
        "stability_errors": stability_errors,
        "iteration_failure": iteration_failure,
    }
    # Runtime executable paths are not part of semantic replay identity.
    for row in rows:
        row["replay"][0] = "python"
    return {
        "kind": "payload-palette-differential-corpus",
        "schema_version": 1,
        "semantic": semantic,
        "semantic_sha256": digest(canonical(semantic)),
        "environment": {**provenance, "per_case_timeout_seconds": timeout},
        "limitations": [
            "matching paths do not prove parser correctness or security",
            "synthetic and selected data, not production prevalence",
            "timeouts depend on the declared runtime; not a speed benchmark",
            "trusted Python/OS and fixed worker; not an RSS or hostile-code sandbox",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "run", "replay", "minimize"))
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--repeats", type=int, default=4)
    parser.add_argument("--chunks", default="1,7,64,4096")
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--case-id")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--attempts", type=int, default=64)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        config = CorpusConfig(
            seed=args.seed,
            repeats=args.repeats,
            chunks=tuple(int(item) for item in args.chunks.split(",")),
            smoke_modulus=16 if args.smoke else 1,
        )
        if args.mode in ("replay", "minimize") and args.case_id is None:
            raise ValueError("replay/minimize requires --case-id")
        if args.mode == "plan":
            result: dict[str, Any] = {
                "version": 1,
                "plan": asdict(corpus_plan(config)),
                "config": asdict(config),
            }
        elif args.mode == "minimize":
            plan = corpus_plan(config)
            provenance = _provenance()
            case = next((case for case in iter_cases(config) if case.case_id == args.case_id), None)
            if case is None:
                raise ValueError("unknown case ID")
            result = minimize_case(
                case,
                config.chunks,
                max_attempts=args.attempts,
                observer=lambda candidate: guarded_compare(candidate, config.chunks, args.timeout),
            )
            result["plan"] = asdict(plan)
            result["configuration"] = asdict(config)
            result["environment"] = {**provenance, "per_case_timeout_seconds": args.timeout}
            result["stability_errors"] = []
            try:
                if corpus_plan(config) != plan:
                    result["stability_errors"].append("corpus_changed")
                if (
                    _provenance()["normalizer_sources_sha256"]
                    != provenance["normalizer_sources_sha256"]
                ):
                    result["stability_errors"].append("normalizer_changed")
            except (OSError, ValueError):
                result["stability_errors"].append("final_source_audit_failed")
        else:
            result = run_corpus(config, timeout=args.timeout, case_id=args.case_id)
        wire = canonical(result) + b"\n"
        if len(wire) > 64 * 1024 * 1024:
            raise ValueError("report exceeds the 64 MiB ceiling")
        with args.output.open("xb") as handle:
            handle.write(wire)
        outcomes = result.get("semantic", {}).get("counts", {}).get("outcome", {})
        return int(
            bool(result.get("semantic", {}).get("stability_errors"))
            or bool(result.get("stability_errors"))
            or any(result.get("counts", {}).get(name, 0) for name in ("timeout", "crash"))
            or any(outcomes.get(name, 0) for name in ("mismatch", "timeout", "crash"))
        )
    except (OSError, ValueError) as error:
        print(f"corpus: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
