"""Private CLI boundary for complete declarative output validation and reports."""

from __future__ import annotations

import argparse
import os
import re
import stat
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import BinaryIO, Literal, NoReturn, TextIO, cast

from payload_palette import __version__
from payload_palette.annotation_adapter import JSONSerializationOptions, _encode_json
from payload_palette.errors import PayloadValidationError, ValidationIssue
from payload_palette.output_config import (
    OutputConfigLimits,
    load_output_config_json_bytes,
    output_config_digest,
)
from payload_palette.output_schema import JSONValue, OutputContractError
from payload_palette.output_validation import OutputReport

OUTPUT_COMMANDS = ("check-output-config", "validate-output")
_DEFAULT_BYTES = 4_000_000
_MAX_BYTES = 32_000_000
_CHUNK = 64 * 1024
_Publication = Literal["none", "unknown", "complete"]
_Stage = Literal["arguments", "config", "input", "report"]
_MESSAGES = {
    "invalid_arguments": "invalid output command arguments",
    "config_read": "configuration file could not be read or closed",
    "invalid_config": "configuration is invalid or exceeds its limits",
    "input_read": "output input could not be read or closed",
    "report_limit": "final report exceeds its byte limit",
    "report_write": "final report could not be written or published",
    "report_cleanup": "owned report temporary file could not be cleaned up",
    "temp_changed": "report temporary identity changed or could not be verified",
    "internal_error": "output command could not be completed",
}
_INPUT_MESSAGES = {
    "input_type": "output input must be a byte sequence",
    "input_too_large": "output input exceeds its byte limit",
    "input_encoding": "output input is not valid UTF-8",
    "invalid_json": "output input is not valid JSON",
    "duplicate_json_key": "output input contains a duplicate JSON key",
    "nonstandard_json_number": "output input contains a nonstandard JSON number",
    "json_number_range": "output input contains an out-of-range JSON number",
    "json_too_deep": "output input exceeds the JSON nesting limit",
    "invalid_unicode": "output input contains invalid Unicode",
    "output_budget": "output input exceeds its value or schema-work limits",
}


class _Failure(Exception):
    def __init__(self, code: str, stage: _Stage, publication: _Publication = "none") -> None:
        self.code = code
        self.stage = stage
        self.publication = publication
        super().__init__(_MESSAGES[code])


def _positive_decimal(value: str) -> int:
    if len(value) > 8 or re.fullmatch(r"[1-9][0-9]*", value) is None:
        raise argparse.ArgumentTypeError("expected a positive decimal integer")
    return int(value)


def _arguments(parser: argparse.ArgumentParser, command: str) -> None:
    parser.add_argument("config", help="explicit UTF-8 JSON configuration file (not stdin)")
    if command == "validate-output":
        parser.add_argument("input", help="UTF-8 JSON model output file, or - for binary stdin")
        parser.add_argument(
            "--include-output",
            action="store_true",
            help="include accepted output; failed output is null",
        )
        parser.add_argument(
            "--max-input-bytes",
            type=_positive_decimal,
            default=_DEFAULT_BYTES,
            help="output input byte limit (default 4000000, ceiling 32000000)",
        )
    parser.add_argument("-o", "--output", default="-", help="new report file, or - for stdout")
    parser.add_argument(
        "--max-config-bytes",
        type=_positive_decimal,
        default=_DEFAULT_BYTES,
        help="configuration byte limit (default/ceiling 4000000)",
    )
    parser.add_argument(
        "--max-report-bytes",
        type=_positive_decimal,
        default=_DEFAULT_BYTES,
        help="report byte limit including LF (default 4000000, ceiling 32000000)",
    )
    parser.add_argument("--version", action="version", version=f"payload-palette {__version__}")


def add_output_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Share grammar with root help; execution uses the separate sanitized parser."""
    for command in OUTPUT_COMMANDS:
        parser = subparsers.add_parser(
            command,
            help=(
                "check declarative output configuration"
                if command == "check-output-config"
                else "validate complete JSON model output"
            ),
            allow_abbrev=False,
        )
        _arguments(parser, command)


def _write_text(stream: TextIO, text: str) -> None:
    offset = 0
    while offset < len(text):
        chunk = text[offset : offset + _CHUNK]
        count = stream.write(chunk)
        if type(count) is not int or not 0 < count <= len(chunk):
            raise OSError("invalid text write progress")
        offset += count
    stream.flush()


class _SafeParser(argparse.ArgumentParser):
    def __init__(self, command: str, stdout: TextIO) -> None:
        super().__init__(prog=f"payload-palette {command}", allow_abbrev=False)
        self._stdout = stdout
        _arguments(self, command)

    def error(self, message: str) -> NoReturn:
        raise _Failure("invalid_arguments", "arguments")

    def _print_message(self, message: str | None, file: object = None) -> None:
        if message:
            _write_text(self._stdout, message)


def _prefer_cleanup(primary: BaseException | None, cleanup: BaseException) -> BaseException:
    # A genuine original control must survive even a second cleanup/control failure.
    return primary if primary is not None and not isinstance(primary, Exception) else cleanup


def _read_bytes(stream: BinaryIO, maximum: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        requested = min(_CHUNK, maximum - size + 1)
        chunk = stream.read(requested)
        if type(chunk) is not bytes or len(chunk) > requested:
            raise OSError("invalid binary read result")
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > maximum:
            raise PayloadValidationError([ValidationIssue("input_too_large", "input too large")])
        chunks.append(chunk)


def _read_path(path: str, maximum: int) -> bytes:
    # A context manager's close could replace a genuine control from the read.
    source = open(path, "rb")  # noqa: SIM115
    primary: BaseException | None = None
    result = b""
    try:
        result = _read_bytes(source, maximum)
    except BaseException as exc:
        primary = exc
    try:
        source.close()
    except BaseException as exc:
        primary = _prefer_cleanup(primary, exc)
    if primary is not None:
        raise primary
    return result


def _rejected(code: str) -> OutputReport:
    safe_code = code if code in _INPUT_MESSAGES else "invalid_json"
    return OutputReport(
        False, (ValidationIssue(safe_code, _INPUT_MESSAGES[safe_code]),), (), 0, None
    )


def _serialize(document: dict[str, JSONValue], maximum: int) -> bytes:
    if maximum <= 1:
        raise _Failure("report_limit", "report")
    try:
        return (
            _encode_json(
                document, JSONSerializationOptions(ensure_ascii=True, max_output_bytes=maximum - 1)
            )
            + b"\n"
        )
    except OutputContractError as exc:
        raise _Failure("report_limit", "report") from exc


def _write_bytes(stream: BinaryIO, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        chunk = payload[offset : offset + _CHUNK]
        count = stream.write(chunk)
        if type(count) is not int or not 0 < count <= len(chunk):
            raise OSError("invalid binary write progress")
        offset += count


def _write_stdout(stdout: TextIO, payload: bytes) -> None:
    try:
        binary = getattr(stdout, "buffer", None)
        if binary is None:
            _write_text(stdout, payload.decode("ascii"))
        else:
            _write_bytes(binary, payload)
            binary.flush()
    except Exception as exc:
        raise _Failure("report_write", "report", "unknown") from exc


def _identity(info: os.stat_result) -> tuple[int, int]:
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise _Failure("temp_changed", "report")
    return info.st_dev, info.st_ino


def _matches(path: Path, identity: tuple[int, int]) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity


def _remove_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) != identity:
        raise _Failure("temp_changed", "report")
    path.unlink()


def _publish_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
    else:
        os.link(source, destination)


def _write_file(destination: Path, payload: bytes) -> None:
    stream: BinaryIO | None = None
    temporary: Path | None = None
    identity: tuple[int, int] | None = None
    publication: _Publication = "none"
    primary: BaseException | None = None
    try:
        # Ownership is settled explicitly so cleanup cannot replace a genuine control.
        stream = cast(
            BinaryIO,
            tempfile.NamedTemporaryFile(  # noqa: SIM115
                mode="wb",
                dir=destination.parent,
                prefix=".payload-output-",
                suffix=".tmp",
                delete=False,
            ),
        )
        temporary = Path(stream.name)
        identity = _identity(os.fstat(stream.fileno()))
        _write_bytes(stream, payload)
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        stream = None
        if not _matches(temporary, identity):
            raise _Failure("temp_changed", "report")
        publication = "unknown"
        _publish_no_replace(temporary, destination)
        publication = "complete"
    except BaseException as exc:
        primary = exc
    if stream is not None:
        try:
            if not stream.closed:
                stream.close()
        except BaseException as exc:
            primary = _prefer_cleanup(primary, exc)
    if temporary is not None and identity is not None:
        try:
            _remove_owned(temporary, identity)
        except BaseException as exc:
            if isinstance(exc, Exception) and not isinstance(exc, _Failure):
                exc = _Failure("report_cleanup", "report")
            primary = _prefer_cleanup(primary, exc)
    if primary is not None:
        if isinstance(primary, Exception):
            code = primary.code if isinstance(primary, _Failure) else "report_write"
            raise _Failure(code, "report", publication) from primary
        raise primary


def _error_notice(failure: _Failure, stderr: TextIO) -> int:
    document: dict[str, JSONValue] = {
        "kind": "payload-output-cli-error",
        "version": 1,
        "valid": False,
        "stage": failure.stage,
        "publication": failure.publication,
        "errors": [{"code": failure.code, "message": _MESSAGES[failure.code], "path": "$"}],
    }
    # The process status remains authoritative when its error channel is unavailable.
    with suppress(Exception):
        _write_stdout(stderr, _serialize(document, 16_384))
    return 2


def run_output_command(
    command: str, argv: list[str], *, stdin: TextIO, stdout: TextIO, stderr: TextIO
) -> int:
    stage: _Stage = "arguments"
    try:
        args = _SafeParser(command, stdout).parse_args(argv)
        if (
            args.config == "-"
            or args.max_config_bytes > _DEFAULT_BYTES
            or args.max_report_bytes > _MAX_BYTES
            or (command == "validate-output" and args.max_input_bytes > _MAX_BYTES)
        ):
            raise _Failure("invalid_arguments", stage)
        stage = "config"
        try:
            config_bytes = _read_path(args.config, args.max_config_bytes)
        except PayloadValidationError as exc:
            raise _Failure("invalid_config", stage) from exc
        except Exception as exc:
            raise _Failure("config_read", stage) from exc
        try:
            pipeline = load_output_config_json_bytes(
                config_bytes, limits=OutputConfigLimits(max_input_bytes=args.max_config_bytes)
            )
            digest = output_config_digest(pipeline)
        except (PayloadValidationError, ValueError) as exc:
            raise _Failure("invalid_config", stage) from exc
        document: dict[str, JSONValue]
        status = 0
        if command == "check-output-config":
            document = {
                "kind": "payload-output-config-check",
                "version": 1,
                "valid": True,
                "config_digest": digest,
                "rule_count": len(pipeline.rules),
            }
        else:
            stage = "input"
            try:
                if args.input == "-":
                    binary = getattr(stdin, "buffer", None)
                    if binary is None:
                        raise _Failure("input_read", stage)
                    raw = _read_bytes(binary, args.max_input_bytes)
                else:
                    raw = _read_path(args.input, args.max_input_bytes)
            except PayloadValidationError as exc:
                report = _rejected(exc.issues[0].code)
            except Exception as exc:
                raise _Failure("input_read", stage) from exc
            else:
                try:
                    report = pipeline.validate_json_bytes(raw, max_input_bytes=args.max_input_bytes)
                except PayloadValidationError as exc:
                    report = _rejected(exc.issues[0].code)
                except OutputContractError:
                    report = _rejected("output_budget")
            status = 0 if report.valid else 1
            document = {
                "kind": "payload-output-validation",
                "version": 1,
                "config_digest": digest,
                **report.to_dict(include_output=args.include_output),
            }
        stage = "report"
        payload = _serialize(document, args.max_report_bytes)
        if args.output == "-":
            _write_stdout(stdout, payload)
        else:
            _write_file(Path(args.output), payload)
        return status
    except _Failure as failure:
        return _error_notice(failure, stderr)
    except Exception:
        return _error_notice(_Failure("internal_error", stage), stderr)
