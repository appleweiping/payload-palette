from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO, StringIO, TextIOWrapper
from pathlib import Path
from typing import Any

import pytest

from payload_palette import (
    OutputSchema,
    RuleBinding,
    StringChoices,
    TrimmedString,
    ValidationPipeline,
    load_output_config,
    output_config_digest,
)
from payload_palette import output_cli as implementation
from payload_palette.cli import run


@pytest.mark.parametrize("command", ["check-output-config", "validate-output"])
def test_public_output_commands_exist_and_accept_null(command: str, tmp_path: Path) -> None:
    configuration = tmp_path / "config.json"
    configuration.write_text(
        '{"kind":"payload-output-config","version":1,"schema":{"type":"null"}}',
        encoding="utf-8",
    )
    args = [command, str(configuration)] + (["-"] if command == "validate-output" else [])
    result = subprocess.run(
        [sys.executable, "-I", "-B", "-m", "payload_palette", *args],
        input=b"null",
        capture_output=True,
        timeout=15,
        check=False,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8")
    assert json.loads(result.stdout)["valid"] is True
    assert result.stderr == b""


def configuration(tmp_path: Path, schema: Any = None, rules: Any = None, **changes: Any) -> Path:
    path = tmp_path / "configuration-private-name.json"
    path.write_text(
        json.dumps(
            {
                "kind": "payload-output-config",
                "version": 1,
                "schema": {"type": "string"} if schema is None else schema,
                "rules": [] if rules is None else rules,
                **changes,
            }
        ),
        encoding="utf-8",
    )
    return path


def invoke(args: list[str], raw: bytes = b'"x"') -> tuple[int, str, str]:
    stdin = TextIOWrapper(BytesIO(raw), encoding="ascii")
    stdout, stderr = StringIO(), StringIO()
    status = run(args, stdin=stdin, stdout=stdout, stderr=stderr)
    assert not stdin.closed and not stdout.closed and not stderr.closed
    return status, stdout.getvalue(), stderr.getvalue()


def error_result(
    result: tuple[int, str, str], code: str, stage: str, publication: str = "none"
) -> dict[str, Any]:
    status, stdout, stderr = result
    assert status == 2 and stdout == ""
    value = json.loads(stderr)
    assert value == {
        "kind": "payload-output-cli-error",
        "version": 1,
        "valid": False,
        "stage": stage,
        "publication": publication,
        "errors": [{"code": code, "message": implementation._MESSAGES[code], "path": "$"}],
    }
    assert stderr.endswith("\n")
    return value


@pytest.mark.parametrize(
    "bad", ["0", "-1", "+1", "01", "1.0", "1e6", " 1", "1 ", "\u0661", "100000000", "private-token"]
)
@pytest.mark.parametrize(
    "option", ["--max-input-bytes", "--max-report-bytes", "--max-config-bytes"]
)
def test_positive_decimal_argument_errors_never_echo_tokens(
    tmp_path: Path, option: str, bad: str
) -> None:
    path = configuration(tmp_path)
    result = invoke(["validate-output", str(path), "-", option, bad])
    error_result(result, "invalid_arguments", "arguments")
    assert "private" not in result[2]


@pytest.mark.parametrize(
    "args",
    [
        ["check-output-config"],
        ["validate-output"],
        ["validate-output", "private-file"],
        ["check-output-config", "-"],
        ["validate-output", "-", "-"],
        ["check-output-config", "private-file", "--include-output"],
        ["check-output-config", "private-file", "--private-option"],
        ["check-output-config", "private-file", "--max-config-bytes", "4000001"],
        ["check-output-config", "private-file", "--max-report-bytes", "32000001"],
        ["validate-output", "private-file", "-", "--max-input-bytes", "32000001"],
        ["validate-output", "private-file", "-", "--max-input", "1"],
    ],
)
def test_missing_unknown_and_oversized_arguments_are_private(args: list[str]) -> None:
    result = invoke(args)
    error_result(result, "invalid_arguments", "arguments")
    assert "private" not in result[2]


@pytest.mark.parametrize("command", implementation.OUTPUT_COMMANDS)
@pytest.mark.parametrize("option", ["--help", "--version"])
def test_help_and_version_use_injected_stdout_without_file_access(
    command: str, option: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_read(*args: Any) -> Any:
        raise AssertionError("file read during help")

    monkeypatch.setattr(implementation, "_read_path", no_read)
    stdout, stderr = StringIO(), StringIO()
    with pytest.raises(SystemExit) as caught:
        run([command, option], stdout=stdout, stderr=stderr)
    assert caught.value.code == 0 and stderr.getvalue() == ""
    assert ("payload-palette 0.5.0" if option == "--version" else command) in stdout.getvalue()


def test_check_report_has_identity_and_never_reads_stdin(tmp_path: Path) -> None:
    path = configuration(tmp_path)

    class NoRead(StringIO):
        @property
        def buffer(self) -> Any:
            raise AssertionError("configuration checker touched stdin")

    stdout, stderr = StringIO(), StringIO()
    assert (
        run(["check-output-config", str(path)], stdin=NoRead(), stdout=stdout, stderr=stderr) == 0
    )
    expected_digest = output_config_digest(load_output_config(json.loads(path.read_text())))
    assert json.loads(stdout.getvalue()) == {
        "kind": "payload-output-config-check",
        "version": 1,
        "valid": True,
        "config_digest": expected_digest,
        "rule_count": 0,
    }
    assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"\xff",
        b'{"kind":"secret","kind":1}',
        b'{"version":NaN}',
        b'{"schema":{"$ref":"private-secret"}}',
        b'{"kind":"payload-output-config","version":99,"schema":{"type":"string"}}',
    ],
)
def test_invalid_config_messages_and_paths_are_private(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "private-config-name"
    path.write_bytes(raw)
    result = invoke(["check-output-config", str(path)])
    error_result(result, "invalid_config", "config")
    assert "private" not in result[2] and "secret" not in result[2]


def test_config_and_input_read_failures_and_config_limit(tmp_path: Path) -> None:
    path = configuration(tmp_path)
    error_result(
        invoke(["check-output-config", str(tmp_path / "private-missing")]), "config_read", "config"
    )
    error_result(
        invoke(["validate-output", str(path), str(tmp_path / "private-missing")]),
        "input_read",
        "input",
    )
    error_result(
        invoke(["check-output-config", str(path), "--max-config-bytes", "1"]),
        "invalid_config",
        "config",
    )
    stdout, stderr = StringIO(), StringIO()
    status = run(
        ["validate-output", str(path), "-"], stdin=StringIO('"x"'), stdout=stdout, stderr=stderr
    )
    error_result((status, stdout.getvalue(), stderr.getvalue()), "input_read", "input")


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"", "invalid_json"),
        (b"\xffprivate", "input_encoding"),
        (b'{"private-secret":1,"private-secret":2}', "duplicate_json_key"),
        (b"NaN", "nonstandard_json_number"),
        (b"1e999", "json_number_range"),
        (b'"\\ud800"', "invalid_unicode"),
        (b"[" * 129, "json_too_deep"),
    ],
)
def test_input_syntax_failures_are_fixed_redacted_rejected_reports(
    tmp_path: Path, raw: bytes, code: str
) -> None:
    path = configuration(tmp_path)
    status, stdout, stderr = invoke(["validate-output", str(path), "-", "--include-output"], raw)
    assert status == 1 and stderr == ""
    report = json.loads(stdout)
    assert report["valid"] is False and report["invocations"] == 0 and report["outcomes"] == []
    assert report["output"] is None
    assert report["issues"] == [
        {"code": code, "message": implementation._INPUT_MESSAGES[code], "path": "$"}
    ]
    assert "private" not in stdout and "secret" not in stdout


def test_input_byte_and_output_work_admission_are_rejected_not_io(tmp_path: Path) -> None:
    path = configuration(tmp_path)
    status, stdout, stderr = invoke(
        ["validate-output", str(path), "-", "--max-input-bytes", "2"], b'"x"'
    )
    assert status == 1 and not stderr
    assert json.loads(stdout)["issues"][0]["code"] == "input_too_large"
    path = configuration(tmp_path, limits={"max_characters": 1})
    status, stdout, stderr = invoke(["validate-output", str(path), "-"], b'"xx"')
    assert status == 1 and not stderr
    assert json.loads(stdout)["issues"] == [
        {
            "code": "output_budget",
            "message": implementation._INPUT_MESSAGES["output_budget"],
            "path": "$",
        }
    ]


def test_complete_repair_report_equals_existing_engine_and_hides_output_by_default(
    tmp_path: Path,
) -> None:
    path = configuration(
        tmp_path,
        {
            "type": "object",
            "properties": {"label": {"type": "string"}, "note": {"type": "string"}},
            "required": ["label"],
            "additionalProperties": False,
        },
        [
            {
                "id": "trim",
                "path": ["label"],
                "validator": {"kind": "trimmed_string"},
                "on_fail": "fix",
            },
            {
                "id": "choose",
                "path": ["label"],
                "validator": {"kind": "string_choices", "choices": ["A"], "fix_case": True},
                "on_fail": "fix",
            },
            {
                "id": "drop",
                "path": ["note"],
                "validator": {"kind": "trimmed_string"},
                "on_fail": "filter",
            },
        ],
    )
    raw = b'{"label":" a ","note":" private-secret "}'
    pipeline = ValidationPipeline(
        OutputSchema(
            "object",
            properties={"label": OutputSchema("string"), "note": OutputSchema("string")},
            required=("label",),
        ),
        (
            RuleBinding("trim", ("label",), TrimmedString(), "fix"),
            RuleBinding("choose", ("label",), StringChoices(("A",), fix_case=True), "fix"),
            RuleBinding("drop", ("note",), TrimmedString(), "filter"),
        ),
    )
    for include_output in (False, True):
        status, stdout, stderr = invoke(
            ["validate-output", str(path), "-", *(["--include-output"] if include_output else [])],
            raw,
        )
        assert status == 0 and stderr == "" and "private-secret" not in stdout
        assert json.loads(stdout) == {
            "kind": "payload-output-validation",
            "version": 1,
            "config_digest": output_config_digest(pipeline),
            **pipeline.validate_json_bytes(raw).to_dict(include_output=include_output),
        }
        assert json.loads(stdout)["invocations"] == 7


def test_normal_diagnostic_paths_and_rule_ids_are_visible_metadata(tmp_path: Path) -> None:
    path = configuration(
        tmp_path,
        {"type": "object", "properties": {"private_field": {"type": "string"}}},
        [
            {
                "id": "private_rule",
                "path": ["private_field"],
                "validator": {"kind": "trimmed_string"},
            }
        ],
    )
    status, stdout, stderr = invoke(
        ["validate-output", str(path), "-"], b'{"private_field":" private-value "}'
    )
    assert status == 1 and stderr == "" and "private-value" not in stdout
    report = json.loads(stdout)
    assert "output" not in report
    assert report["issues"][0]["path"] == '$["private_field"]'
    assert report["outcomes"][0]["rule_id"] == "private_rule"


def test_later_repair_required_filter_and_null_cli_oracles(tmp_path: Path) -> None:
    path = configuration(
        tmp_path,
        rules=[
            {"id": "lower", "path": [], "validator": {"kind": "string_choices", "choices": ["a"]}},
            {
                "id": "upper",
                "path": [],
                "validator": {"kind": "string_choices", "choices": ["A"], "fix_case": True},
                "on_fail": "fix",
            },
        ],
    )
    status, stdout, stderr = invoke(["validate-output", str(path), "-", "--include-output"], b'"a"')
    result = json.loads(stdout)
    assert status == 1 and not stderr and result["output"] is None and result["invocations"] == 5
    assert [item["status"] for item in result["outcomes"]] == [
        "passed",
        "fixed",
        "rejected",
        "passed",
    ]
    path = configuration(
        tmp_path,
        {"type": "object", "properties": {"label": {"type": "string"}}, "required": ["label"]},
        [
            {
                "id": "drop",
                "path": ["label"],
                "validator": {"kind": "trimmed_string"},
                "on_fail": "filter",
            }
        ],
    )
    status, stdout, _ = invoke(["validate-output", str(path), "-"], b'{"label":" a "}')
    assert status == 1 and json.loads(stdout)["invocations"] == 1
    assert json.loads(stdout)["issues"][0]["code"] == "schema_required"
    path = configuration(tmp_path, {"type": "null"})
    status, stdout, _ = invoke(["validate-output", str(path), "-", "--include-output"], b"null")
    assert (
        status == 0
        and json.loads(stdout)["output"] is None
        and json.loads(stdout)["invocations"] == 0
    )


def test_report_cap_counts_lf_and_serializes_before_temp_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configuration(tmp_path)
    _, original, _ = invoke(["check-output-config", str(path)])
    size = len(original.encode("ascii"))
    assert (
        invoke(["check-output-config", str(path), "--max-report-bytes", str(size)])[1] == original
    )

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("temporary file created before report admission")

    monkeypatch.setattr(implementation.tempfile, "NamedTemporaryFile", forbidden)
    for cap in (1, size - 1):
        result = invoke(
            [
                "check-output-config",
                str(path),
                "-o",
                str(tmp_path / "report"),
                "--max-report-bytes",
                str(cap),
            ]
        )
        error_result(result, "report_limit", "report")
    assert not (tmp_path / "report").exists()


def test_bounded_reads_handle_short_chunks_and_stop_at_limit_plus_one() -> None:
    class Chunks(BytesIO):
        requests: list[int]

        def __init__(self, payload: bytes) -> None:
            super().__init__(payload)
            self.requests = []

        def read(self, n: int = -1) -> bytes:
            self.requests.append(n)
            return super().read(min(n, 2))

    source = Chunks(b"abcde")
    assert implementation._read_bytes(source, 5) == b"abcde"
    assert source.requests == [6, 4, 2, 1]
    source = Chunks(b"abcdefgh")
    with pytest.raises(Exception, match="input_too_large"):
        implementation._read_bytes(source, 4)
    assert source.tell() == 5
    assert source.requests == [5, 3, 1]


@pytest.mark.parametrize("value", [None, "", False, b"too many bytes"])
def test_malformed_reader_results_are_io_failures(value: Any) -> None:
    class Source:
        def read(self, n: int) -> Any:
            return value

    with pytest.raises(OSError):
        implementation._read_bytes(Source(), 1)  # type: ignore[arg-type]


def test_owned_reads_preserve_controls_and_close_errors_override_data_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interrupt = KeyboardInterrupt("private-control")

    class Source(BytesIO):
        attempts = 0

        def read(self, n: int = -1) -> bytes:
            raise interrupt

        def close(self) -> None:
            self.attempts += 1
            super().close()
            raise OSError("private-close")

    source = Source()
    monkeypatch.setattr(implementation, "open", lambda *args: source, raising=False)
    with pytest.raises(KeyboardInterrupt) as caught:
        implementation._read_path("private", 4)
    assert caught.value is interrupt and source.closed and source.attempts == 1

    class CloseFailure(BytesIO):
        def close(self) -> None:
            super().close()
            raise OSError("private-close")

    oversized = CloseFailure(b"too large")
    monkeypatch.setattr(implementation, "open", lambda *args: oversized, raising=False)
    with pytest.raises(OSError, match="private-close"):
        implementation._read_path("private", 1)
    assert oversized.closed


def test_binary_stdout_flushes_without_text_translation_or_caller_close(tmp_path: Path) -> None:
    path = configuration(tmp_path)
    stdout, stderr = TextIOWrapper(BytesIO(), encoding="utf-16"), StringIO()
    assert run(["check-output-config", str(path)], stdout=stdout, stderr=stderr) == 0
    assert not stdout.closed and not stderr.getvalue()
    raw = stdout.buffer.getvalue()
    assert raw.isascii() and raw.endswith(b"\n") and b"\r" not in raw
    assert json.loads(raw)["valid"] is True


def test_publication_error_overrides_rejected_output_exit(tmp_path: Path) -> None:
    path = configuration(tmp_path)
    destination = tmp_path / "existing-report"
    destination.write_bytes(b"owned by someone else")
    result = invoke(["validate-output", str(path), "-", "-o", str(destination)], b"null")
    error_result(result, "report_write", "report", "unknown")
    assert destination.read_bytes() == b"owned by someone else"


def test_offline_configured_output_example_runs_as_a_real_process() -> None:
    example = Path(__file__).resolve().parents[1] / "examples" / "validate_configured_output.py"
    result = subprocess.run(
        [sys.executable, "-I", "-B", str(example)],
        capture_output=True,
        timeout=15,
        check=False,
        cwd=example.parent,
    )
    assert result.returncode == 0 and result.stderr == b""
    report = json.loads(result.stdout)
    assert report["kind"] == "payload-output-validation"
    assert report["valid"] is True and report["output"] == "A"
    # Two initial checks, two immediate repair rechecks, then two final checks.
    assert report["invocations"] == 6


def test_unexpected_ordinary_errors_and_failed_error_channel_are_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = configuration(tmp_path)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("private-secret")

    monkeypatch.setattr(implementation, "output_config_digest", fail)
    result = invoke(["check-output-config", str(path)])
    error_result(result, "internal_error", "config")
    assert "private-secret" not in result[2]

    class Broken(StringIO):
        def write(self, text: str) -> int:
            raise OSError("private-stderr")

    assert run(["check-output-config"], stdout=StringIO(), stderr=Broken()) == 2


def test_stdout_flush_failure_is_not_success_and_never_closes_caller_stream(tmp_path: Path) -> None:
    class BadFlush(StringIO):
        def flush(self) -> None:
            raise OSError("private-flush")

    path = configuration(tmp_path)
    stdout, stderr = BadFlush(), StringIO()
    assert run(["check-output-config", str(path)], stdout=stdout, stderr=stderr) == 2
    assert json.loads(stderr.getvalue())["publication"] == "unknown"
    assert not stdout.closed and "private" not in stderr.getvalue()


def test_real_unicode_input_and_report_files_use_ascii_json_lf(tmp_path: Path) -> None:
    path = configuration(tmp_path)
    source, destination = tmp_path / "输入.json", tmp_path / "报告.json"
    source.write_text(json.dumps("😀é", ensure_ascii=False), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            "-m",
            "payload_palette",
            "validate-output",
            str(path),
            str(source),
            "-o",
            str(destination),
            "--include-output",
        ],
        capture_output=True,
        check=False,
        timeout=15,
        cwd=tmp_path,
    )
    assert result.returncode == 0 and not result.stdout and not result.stderr
    payload = destination.read_bytes()
    assert payload.endswith(b"\n") and b"\r\n" not in payload and payload.isascii()
    assert json.loads(payload)["output"] == "😀é"
    assert not list(tmp_path.glob(".payload-output-*.tmp"))


@pytest.mark.parametrize("target", ["config", "input", "directory", "existing"])
def test_existing_report_targets_and_aliases_are_never_modified(
    tmp_path: Path, target: str
) -> None:
    config_path = configuration(tmp_path)
    input_path = tmp_path / "input.json"
    input_path.write_bytes(b'"x"')
    existing = tmp_path / "existing.json"
    existing.write_bytes(b"original-report")
    destination = {
        "config": config_path,
        "input": input_path,
        "directory": tmp_path,
        "existing": existing,
    }[target]
    before = None if destination.is_dir() else destination.read_bytes()
    result = invoke(["validate-output", str(config_path), str(input_path), "-o", str(destination)])
    error_result(result, "report_write", "report", "unknown")
    assert destination.is_dir() if before is None else destination.read_bytes() == before
    assert not list(tmp_path.glob(".payload-output-*.tmp"))


def test_two_real_processes_have_exactly_one_complete_publication_winner(tmp_path: Path) -> None:
    path = configuration(tmp_path)
    destination = tmp_path / "race.json"
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-B",
                "-m",
                "payload_palette",
                "validate-output",
                str(path),
                "-",
                "-o",
                str(destination),
                "--include-output",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=tmp_path,
        )
        for _ in range(2)
    ]
    try:
        assert processes[0].pid != processes[1].pid
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(process.communicate, json.dumps(value).encode(), timeout=15)
                for process, value in zip(processes, ("winner-a", "winner-b"), strict=True)
            ]
            outputs = [future.result(timeout=20) for future in futures]
        assert sorted(process.returncode for process in processes) == [0, 2]
        report = json.loads(destination.read_bytes())
        assert report["valid"] is True and report["output"] in ("winner-a", "winner-b")
        winner = next(index for index, process in enumerate(processes) if process.returncode == 0)
        assert report["output"] == ("winner-a", "winner-b")[winner]
        assert outputs[winner] == (b"", b"")
        assert json.loads(outputs[1 - winner][1])["errors"][0]["code"] == "report_write"
        assert not list(tmp_path.glob(".payload-output-*.tmp"))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=15)


def test_platform_no_replace_primitive_uses_native_contract(tmp_path: Path) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(b"source")
    destination.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        implementation._publish_no_replace(source, destination)
    assert destination.read_bytes() == b"original" and source.read_bytes() == b"source"
    destination.unlink()
    implementation._publish_no_replace(source, destination)
    assert destination.read_bytes() == b"source"
    assert source.exists() is (os.name != "nt")
