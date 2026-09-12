from __future__ import annotations

import errno
import json
import os
from io import BytesIO, StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from payload_palette import output_cli as implementation

PAYLOAD = b'{"valid":true}\n'


def fail_with(error: BaseException):
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail


class FileProxy:
    def __init__(self, source: Any, stage: str, error: BaseException) -> None:
        self.source = source
        self.name = source.name
        self.stage = stage
        self.error = error
        self.triggered = False
        self.close_calls = 0

    @property
    def closed(self) -> bool:
        return bool(self.source.closed)

    def fileno(self) -> int:
        return int(self.source.fileno())

    def write(self, value: bytes) -> int:
        if self.stage == "write" and not self.triggered:
            self.triggered = True
            self.source.write(value[:3])
            raise self.error
        return int(self.source.write(value))

    def flush(self) -> None:
        if self.stage == "flush" and not self.triggered:
            self.triggered = True
            raise self.error
        self.source.flush()

    def close(self) -> None:
        self.close_calls += 1
        if self.stage == "close_before" and not self.triggered:
            self.triggered = True
            raise self.error
        self.source.close()
        if self.stage == "close_after" and not self.triggered:
            self.triggered = True
            raise self.error


def proxy_factory(
    monkeypatch: pytest.MonkeyPatch, stage: str, error: BaseException
) -> list[FileProxy]:
    original = implementation.tempfile.NamedTemporaryFile
    created: list[FileProxy] = []

    def create(*args: Any, **kwargs: Any) -> FileProxy:
        proxy = FileProxy(original(*args, **kwargs), stage, error)
        created.append(proxy)
        return proxy

    monkeypatch.setattr(implementation.tempfile, "NamedTemporaryFile", create)
    return created


@pytest.mark.parametrize(
    "stage", ["write", "flush", "close_before", "close_after", "fsync", "publish"]
)
@pytest.mark.parametrize("kind", ["ordinary", "keyboard", "system", "generator", "group"])
def test_write_failures_and_controls_settle_owned_file_without_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, kind: str
) -> None:
    errors: dict[str, BaseException] = {
        "ordinary": OSError("private-disk"),
        "keyboard": KeyboardInterrupt("private-control"),
        "system": SystemExit(7),
        "generator": GeneratorExit("private-control"),
        "group": BaseExceptionGroup("private-group", [KeyboardInterrupt("private-control")]),
    }
    error = errors[kind]
    created = proxy_factory(monkeypatch, stage, error)
    if stage == "fsync":
        monkeypatch.setattr(implementation.os, "fsync", fail_with(error))
    elif stage == "publish":
        monkeypatch.setattr(implementation, "_publish_no_replace", fail_with(error))
    destination = tmp_path / "report.json"
    if kind == "ordinary":
        with pytest.raises(implementation._Failure) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value.code == "report_write"
        assert caught.value.publication == ("unknown" if stage == "publish" else "none")
        assert "private" not in str(caught.value)
    else:
        with pytest.raises(type(error)) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value is error
    assert len(created) == 1 and created[0].closed
    assert created[0].close_calls == (2 if stage == "close_before" else 1)
    assert not destination.exists() and not list(tmp_path.glob(".payload-output-*.tmp"))


@pytest.mark.parametrize("kind", ["ordinary", "control"])
def test_publication_then_failure_never_deletes_complete_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    original = implementation._publish_no_replace
    error = (
        OSError("private-acknowledgement")
        if kind == "ordinary"
        else KeyboardInterrupt("private-control")
    )
    destination = tmp_path / "report.json"

    def publish(source: Path, target: Path) -> None:
        original(source, target)
        raise error

    monkeypatch.setattr(implementation, "_publish_no_replace", publish)
    if kind == "ordinary":
        with pytest.raises(implementation._Failure) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value.publication == "unknown"
    else:
        with pytest.raises(KeyboardInterrupt) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value is error
    assert destination.read_bytes() == PAYLOAD
    assert not list(tmp_path.glob(".payload-output-*.tmp"))


def test_existing_target_race_is_rejected_at_native_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = implementation._publish_no_replace
    destination = tmp_path / "report.json"

    def raced(source: Path, target: Path) -> None:
        assert source.read_bytes() == PAYLOAD and not target.exists()
        target.write_bytes(b"concurrent-owner")
        original(source, target)

    monkeypatch.setattr(implementation, "_publish_no_replace", raced)
    with pytest.raises(implementation._Failure) as caught:
        implementation._write_file(destination, PAYLOAD)
    assert caught.value.publication == "unknown"
    assert destination.read_bytes() == b"concurrent-owner"
    assert not list(tmp_path.glob(".payload-output-*.tmp"))


def test_actual_publication_occurs_only_after_complete_flush_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = proxy_factory(monkeypatch, "none", OSError())
    original = implementation._publish_no_replace
    destination = tmp_path / "report.json"

    def check(source: Path, target: Path) -> None:
        assert created[0].closed and created[0].close_calls == 1
        assert source.read_bytes() == PAYLOAD and not target.exists()
        original(source, target)
        assert target.read_bytes() == PAYLOAD

    monkeypatch.setattr(implementation, "_publish_no_replace", check)
    implementation._write_file(destination, PAYLOAD)


def test_foreign_temp_replacement_before_publication_is_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = implementation._matches
    renamed = tmp_path / "moved-owned-file"
    foreign: list[Path] = []

    def replace(source: Path, identity: tuple[int, int]) -> bool:
        source.rename(renamed)
        source.write_bytes(b"foreign-owner")
        foreign.append(source)
        return original(source, identity)

    monkeypatch.setattr(implementation, "_matches", replace)
    destination = tmp_path / "report.json"
    with pytest.raises(implementation._Failure) as caught:
        implementation._write_file(destination, PAYLOAD)
    assert caught.value.code == "temp_changed" and caught.value.publication == "none"
    assert not destination.exists() and renamed.read_bytes() == PAYLOAD
    assert foreign[0].read_bytes() == b"foreign-owner"


def test_foreign_temp_replacement_after_publication_preserves_both_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = implementation._publish_no_replace
    foreign: list[Path] = []

    def publish(source: Path, target: Path) -> None:
        original(source, target)
        if source.exists():
            source.unlink()
        source.write_bytes(b"foreign-owner")
        foreign.append(source)

    monkeypatch.setattr(implementation, "_publish_no_replace", publish)
    destination = tmp_path / "report.json"
    with pytest.raises(implementation._Failure) as caught:
        implementation._write_file(destination, PAYLOAD)
    assert caught.value.code == "temp_changed" and caught.value.publication == "complete"
    assert destination.read_bytes() == PAYLOAD and foreign[0].read_bytes() == b"foreign-owner"


@pytest.mark.parametrize(
    "cleanup", [OSError("private-unlink"), KeyboardInterrupt("private-cleanup")]
)
def test_post_publication_cleanup_failure_is_explicit_and_never_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup: BaseException
) -> None:
    # Real hard links on this host exercise the POSIX-shaped two-name cleanup state.
    monkeypatch.setattr(implementation, "_publish_no_replace", os.link)
    monkeypatch.setattr(implementation, "_remove_owned", fail_with(cleanup))
    destination = tmp_path / "report.json"
    if isinstance(cleanup, Exception):
        with pytest.raises(implementation._Failure) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value.code == "report_cleanup" and caught.value.publication == "complete"
    else:
        with pytest.raises(KeyboardInterrupt) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value is cleanup
    assert destination.read_bytes() == PAYLOAD
    names = list(tmp_path.glob(".payload-output-*.tmp"))
    assert len(names) == 1 and names[0].read_bytes() == PAYLOAD


def test_original_control_survives_secondary_close_and_cleanup_controls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = KeyboardInterrupt("original")
    created = proxy_factory(monkeypatch, "write", original)
    monkeypatch.setattr(implementation, "_remove_owned", fail_with(SystemExit(9)))
    with pytest.raises(KeyboardInterrupt) as caught:
        implementation._write_file(tmp_path / "report", PAYLOAD)
    assert caught.value is original and created[0].closed


@pytest.mark.parametrize("control", [False, True])
def test_cleanup_close_failure_preserves_original_control_and_never_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, control: bool
) -> None:
    original = KeyboardInterrupt("private-original") if control else OSError("private-write")
    created = proxy_factory(monkeypatch, "write", original)

    def failed_close(proxy: FileProxy) -> None:
        proxy.source.close()
        raise OSError("private-cleanup-close")

    monkeypatch.setattr(FileProxy, "close", failed_close)
    destination = tmp_path / "report"
    if control:
        with pytest.raises(KeyboardInterrupt) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value is original
    else:
        with pytest.raises(implementation._Failure) as caught:
            implementation._write_file(destination, PAYLOAD)
        assert caught.value.code == "report_write" and caught.value.publication == "none"
    assert created[0].closed and not destination.exists()
    assert not list(tmp_path.glob(".payload-output-*.tmp"))


def test_cleanup_control_overrides_ordinary_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = proxy_factory(monkeypatch, "write", OSError("private-write"))
    cleanup = KeyboardInterrupt("private-cleanup")
    monkeypatch.setattr(implementation, "_remove_owned", fail_with(cleanup))
    with pytest.raises(KeyboardInterrupt) as caught:
        implementation._write_file(tmp_path / "report", PAYLOAD)
    assert caught.value is cleanup and created[0].closed


def test_identity_failures_cannot_authorize_path_only_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created = proxy_factory(monkeypatch, "none", OSError())
    original = implementation.os.fstat

    def no_identity(fd: int) -> Any:
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=0)

    monkeypatch.setattr(implementation.os, "fstat", no_identity)
    with pytest.raises(implementation._Failure) as caught:
        implementation._write_file(tmp_path / "report", PAYLOAD)
    assert caught.value.code == "temp_changed" and caught.value.publication == "none"
    assert created[0].closed
    assert Path(created[0].name).exists()
    assert not (tmp_path / "report").exists()


def test_missing_temporary_name_is_not_published_or_deleted_elsewhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = implementation._matches

    def remove(source: Path, identity: tuple[int, int]) -> bool:
        source.unlink()
        return original(source, identity)

    monkeypatch.setattr(implementation, "_matches", remove)
    with pytest.raises(implementation._Failure, match="identity"):
        implementation._write_file(tmp_path / "report", PAYLOAD)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("count", [None, True, 0, -1, 999])
def test_binary_and_text_invalid_write_progress_is_not_accepted(count: Any) -> None:
    class Bytes:
        def write(self, chunk: bytes) -> Any:
            return count

    class Text(StringIO):
        def write(self, text: str) -> Any:
            return count

    with pytest.raises(OSError):
        implementation._write_bytes(Bytes(), PAYLOAD)  # type: ignore[arg-type]
    with pytest.raises(OSError):
        implementation._write_text(Text(), "report")


def test_short_binary_and_text_writes_finish_without_losing_bytes() -> None:
    class ShortBytes(BytesIO):
        def write(self, chunk: Any) -> int:
            return super().write(chunk[:2])

    class ShortText(StringIO):
        def write(self, text: str) -> int:
            return super().write(text[:2])

    binary, text = ShortBytes(), ShortText()
    implementation._write_bytes(binary, PAYLOAD)
    implementation._write_text(text, PAYLOAD.decode())
    assert binary.getvalue() == PAYLOAD and text.getvalue() == PAYLOAD.decode()


def test_native_posix_link_adapter_and_unsupported_filesystem_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, destination = tmp_path / "source", tmp_path / "destination"
    source.write_bytes(PAYLOAD)
    monkeypatch.setattr(implementation, "os", SimpleNamespace(name="posix", link=os.link))
    implementation._publish_no_replace(source, destination)
    assert source.read_bytes() == destination.read_bytes() == PAYLOAD
    with pytest.raises(FileExistsError):
        implementation._publish_no_replace(source, destination)
    monkeypatch.setattr(
        implementation,
        "os",
        SimpleNamespace(
            name="posix", link=fail_with(OSError(errno.EOPNOTSUPP, "private-filesystem"))
        ),
    )
    with pytest.raises(OSError):
        implementation._publish_no_replace(source, tmp_path / "unsupported")
    assert not (tmp_path / "unsupported").exists()


@pytest.mark.parametrize("dangling", [False, True])
def test_existing_symlink_destination_is_never_followed_or_replaced(
    tmp_path: Path, dangling: bool
) -> None:
    target, destination = tmp_path / "target", tmp_path / "report"
    if not dangling:
        target.write_bytes(b"original")
    try:
        destination.symlink_to(target)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows host lacks symlink creation privilege")
        raise
    with pytest.raises(implementation._Failure):
        implementation._write_file(destination, PAYLOAD)
    assert destination.is_symlink()
    assert not target.exists() if dangling else target.read_bytes() == b"original"


def test_stderr_control_is_propagated_not_silently_reported_as_exit_two() -> None:
    control = KeyboardInterrupt("private-control")

    class Controlled(StringIO):
        def write(self, value: str) -> int:
            raise control

    with pytest.raises(KeyboardInterrupt) as caught:
        implementation._error_notice(
            implementation._Failure("invalid_arguments", "arguments"), Controlled()
        )
    assert caught.value is control


def test_error_document_is_bounded_and_does_not_contain_exception_context() -> None:
    stderr = StringIO()
    failure = implementation._Failure("report_write", "report", "unknown")
    failure.__cause__ = RuntimeError("private-secret")
    assert implementation._error_notice(failure, stderr) == 2
    assert len(stderr.getvalue()) < 16_384 and "private-secret" not in stderr.getvalue()
    assert json.loads(stderr.getvalue())["publication"] == "unknown"
