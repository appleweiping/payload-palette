"""Command-line interface for validation and manifest generation."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import IO, Any, TextIO

from payload_palette import __version__
from payload_palette.errors import OutputError, PayloadValidationError, ValidationIssue
from payload_palette.ingress import (
    DEFAULT_MAX_INPUT_BYTES,
    MAX_INGRESS_INPUT_BYTES,
    _decode_json_text,
    _too_large,
    validate_input_limit,
)
from payload_palette.ingress import (
    MAX_JSON_DEPTH as _MAX_JSON_DEPTH,
)
from payload_palette.ingress import (
    MAX_JSON_INTEGER_DIGITS as _MAX_JSON_INTEGER_DIGITS,
)
from payload_palette.models import Manifest
from payload_palette.normalizer import normalize
from payload_palette.output_cli import OUTPUT_COMMANDS, add_output_commands, run_output_command
from payload_palette.policy import ENVELOPE_NAMES, NormalizationPolicy, RemoteURLPolicy
from payload_palette.streaming import normalize_path, normalize_stream

MAX_CLI_INPUT_BYTES = MAX_INGRESS_INPUT_BYTES
MAX_JSON_DEPTH = _MAX_JSON_DEPTH
MAX_JSON_INTEGER_DIGITS = _MAX_JSON_INTEGER_DIGITS
_READ_CHUNK_CHARACTERS = 64 * 1024


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="payload-palette",
        description="Validate ordered multimodal payloads without fetching remote media.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate and summarize a payload")
    normalize_parser = subparsers.add_parser(
        "normalize", help="write a canonical, binary-free manifest"
    )
    for command in (validate_parser, normalize_parser):
        command.add_argument("input", help="JSON input file, or - for standard input")
        command.add_argument(
            "--allow-host",
            action="append",
            default=[],
            metavar="HOST",
            help="allow an exact remote host or an explicit wildcard such as *.example.org",
        )
        command.add_argument(
            "--allow-http", action="store_true", help="allow HTTP for allowlisted hosts"
        )
        command.add_argument(
            "--keep-url-query",
            action="store_true",
            help="include remote URL query strings in manifests (may expose secrets)",
        )
        command.add_argument("--max-parts", type=int, default=128)
        command.add_argument("--max-total-bytes", type=int, default=128 * 1024 * 1024)
        command.add_argument(
            "--max-input-bytes",
            type=int,
            default=DEFAULT_MAX_INPUT_BYTES,
            help=f"maximum UTF-8 JSON input size (hard ceiling {MAX_CLI_INPUT_BYTES} bytes)",
        )
        command.add_argument(
            "--no-signature-check",
            action="store_true",
            help="skip best-effort media signature comparison",
        )
        command.add_argument(
            "--allow-url-safe-base64",
            action="store_true",
            help="also accept RFC 4648 section 5 URL-safe Base64 inline media",
        )
        command.add_argument(
            "--envelope",
            choices=ENVELOPE_NAMES,
            default="default",
            help="request envelope to read; vendor shapes are never auto-detected",
        )
        command.add_argument(
            "--stream",
            action="store_true",
            help=(
                "decode incrementally so peak memory follows the request structure "
                "rather than its media size"
            ),
        )
        command.add_argument("--json-errors", action="store_true")
    normalize_parser.add_argument("-o", "--output", default="-", help="output path or -")
    normalize_parser.add_argument("--compact", action="store_true", help="emit compact JSON")
    add_output_commands(subparsers)
    return parser


def _policy(args: argparse.Namespace) -> NormalizationPolicy:
    return NormalizationPolicy(
        max_parts=args.max_parts,
        max_total_inline_bytes=args.max_total_bytes,
        verify_known_signatures=not args.no_signature_check,
        allow_url_safe_base64=args.allow_url_safe_base64,
        envelope=args.envelope,
        remote=RemoteURLPolicy(
            allowed_hosts=tuple(args.allow_host),
            require_https=not args.allow_http,
            redact_query=not args.keep_url_query,
        ),
    )


def _validate_input_limit(value: object) -> int:
    return validate_input_limit(value)


def _read_json(path: str, stdin: TextIO, max_input_bytes: int = DEFAULT_MAX_INPUT_BYTES) -> Any:
    maximum = _validate_input_limit(max_input_bytes)
    try:
        text = (
            _read_stream_bounded(stdin, maximum)
            if path == "-"
            else _read_file_bounded(Path(path), maximum)
        )
    except UnicodeError as exc:
        raise PayloadValidationError(
            [ValidationIssue("input_encoding", f"input is not valid UTF-8: {exc}", path)]
        ) from exc
    except OSError as exc:
        raise PayloadValidationError(
            [ValidationIssue("input_read", f"cannot read input: {exc}", path)]
        ) from exc
    return _decode_json_text(text)


def _read_stream_bounded(stream: TextIO, maximum: int) -> str:
    chunks: list[str] = []
    byte_count = 0
    while True:
        chunk = stream.read(_READ_CHUNK_CHARACTERS)
        if not chunk:
            break
        if not isinstance(chunk, str):
            raise OSError("standard input must be a text stream")
        byte_count += len(chunk.encode("utf-8"))
        if byte_count > maximum:
            raise PayloadValidationError(
                [
                    ValidationIssue(
                        "input_too_large",
                        f"JSON input exceeds the {maximum}-byte limit",
                        "$",
                    )
                ]
            )
        chunks.append(chunk)
    return "".join(chunks)


def _read_file_bounded(path: Path, maximum: int) -> str:
    with path.open("rb") as stream:
        payload = stream.read(maximum + 1)
    if len(payload) > maximum:
        raise _too_large(maximum, str(path))
    return payload.decode("utf-8")


def _json_encoder(compact: bool, ensure_ascii: bool) -> json.JSONEncoder:
    separators = (",", ":") if compact else None
    indent = None if compact else 2
    return json.JSONEncoder(
        ensure_ascii=ensure_ascii,
        indent=indent,
        separators=separators,
        allow_nan=False,
    )


def _write_json(value: Any, stream: IO[str], compact: bool = False) -> None:
    encoder = _json_encoder(compact, ensure_ascii=False)
    encoding = getattr(stream, "encoding", None)
    try:
        for chunk in encoder.iterencode(value):
            if encoding is not None:
                chunk.encode(encoding)
    except UnicodeEncodeError:
        encoder = _json_encoder(compact, ensure_ascii=True)
    for chunk in encoder.iterencode(value):
        stream.write(chunk)
    stream.write("\n")


def _write_text(value: str, stream: TextIO) -> None:
    encoding = getattr(stream, "encoding", None)
    if encoding is not None:
        try:
            value.encode(encoding)
        except UnicodeEncodeError:
            value = value.encode(encoding, errors="backslashreplace").decode(encoding)
    stream.write(value)


def _write_json_to_stream(
    value: Any,
    stream: TextIO,
    *,
    compact: bool = False,
    path: str = "-",
) -> None:
    try:
        _write_json(value, stream, compact=compact)
    except (OSError, UnicodeError) as exc:
        raise OutputError(path, f"cannot write output: {exc}") from exc


def _write_json_file_atomic(value: Any, path: Path, *, compact: bool = False) -> None:
    """Write JSON through a same-directory temporary file and atomically replace the target."""

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name or 'payload-palette'}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            _write_json(value, stream, compact=compact)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, UnicodeError) as exc:
        raise OutputError(str(path), f"cannot write output: {exc}") from exc
    finally:
        if temporary_path is not None:
            with suppress(OSError):
                temporary_path.unlink(missing_ok=True)


def _normalize_streamed(
    path: str, stdin: TextIO, policy: NormalizationPolicy, max_input_bytes: int
) -> Manifest:
    """Normalize without buffering the complete raw JSON request body.

    Streaming needs the raw bytes, not decoded text, so standard input is read
    through its binary buffer.  A text stream with no buffer is refused rather
    than re-encoded, because re-encoding would reintroduce the whole-request
    copy the caller asked to avoid. Ordinary structure and accepted text are
    still materialized; long media strings are summarized while they stream.
    """

    if path != "-":
        return normalize_path(Path(path), policy, max_input_bytes=max_input_bytes)
    buffer = getattr(stdin, "buffer", None)
    if buffer is None:
        raise PayloadValidationError(
            [
                ValidationIssue(
                    "input_read",
                    "--stream needs a binary standard input; pass a file path instead",
                    "-",
                )
            ]
        )
    return normalize_stream(buffer, policy, max_input_bytes=max_input_bytes)


def run(
    argv: list[str] | None = None,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    """Run the CLI with injectable streams and return a process exit code."""

    active_argv = sys.argv[1:] if argv is None else argv
    if active_argv and active_argv[0] in OUTPUT_COMMANDS:
        return run_output_command(
            active_argv[0], active_argv[1:], stdin=stdin, stdout=stdout, stderr=stderr
        )
    args = _parser().parse_args(active_argv)
    try:
        active_policy = _policy(args)
        input_limit = _validate_input_limit(args.max_input_bytes)
    except ValueError as exc:
        return _report_error(
            PayloadValidationError([ValidationIssue("invalid_policy", str(exc), "$")]),
            args.json_errors,
            stderr,
        )
    try:
        if args.stream:
            manifest = _normalize_streamed(args.input, stdin, active_policy, input_limit)
        else:
            manifest = normalize(_read_json(args.input, stdin, input_limit), active_policy)
        if args.command == "validate":
            result = {
                "valid": True,
                "fingerprint": manifest.fingerprint,
                "part_count": manifest.part_count,
                "inline_bytes": manifest.inline_bytes,
            }
            _write_json_to_stream(result, stdout)
        else:
            if args.output == "-":
                _write_json_to_stream(manifest.to_dict(), stdout, compact=args.compact, path="-")
            else:
                _write_json_file_atomic(manifest.to_dict(), Path(args.output), compact=args.compact)
    except PayloadValidationError as exc:
        return _report_error(exc, args.json_errors, stderr)
    except OutputError as exc:
        return _report_error(
            PayloadValidationError([ValidationIssue("output_write", str(exc), exc.path)]),
            args.json_errors,
            stderr,
        )
    return 0


def _report_error(error: PayloadValidationError, as_json: bool, stderr: TextIO) -> int:
    if as_json:
        _write_json(error.to_dict(), stderr)
    else:
        _write_text(f"payload-palette: {error}\n", stderr)
    return 2


def main() -> None:
    """Console-script entry point."""

    raise SystemExit(run())
