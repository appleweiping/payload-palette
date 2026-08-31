"""Structured validation errors with stable codes and JSON paths."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One actionable problem in a request payload.

    ``path`` uses a compact JSONPath-like notation so callers can map an issue
    back to an editor or API field without parsing prose.
    """

    code: str
    message: str
    path: str = "$"
    hint: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation without null fields."""

        return {key: value for key, value in asdict(self).items() if value is not None}


class PayloadValidationError(ValueError):
    """Raised when one or more payload validation rules fail."""

    def __init__(self, issues: Iterable[ValidationIssue]) -> None:
        collected = tuple(issues)
        if not collected:
            raise ValueError("PayloadValidationError requires at least one issue")
        self.issues = collected
        super().__init__(self._render())

    def _render(self) -> str:
        return "; ".join(f"{issue.path}: [{issue.code}] {issue.message}" for issue in self.issues)

    def to_dict(self) -> dict[str, Any]:
        """Return the stable error envelope used by the CLI."""

        return {
            "valid": False,
            "error_count": len(self.issues),
            "errors": [issue.to_dict() for issue in self.issues],
        }


class OutputError(OSError):
    """Raised when a validated result cannot be written safely."""

    def __init__(self, path: str, message: str) -> None:
        self.path = path
        super().__init__(message)


def problem(code: str, message: str, path: str, hint: str | None = None) -> PayloadValidationError:
    """Build a one-issue validation exception."""

    return PayloadValidationError(
        [ValidationIssue(code=code, message=message, path=path, hint=hint)]
    )
