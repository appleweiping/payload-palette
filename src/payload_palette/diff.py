"""Deterministic manifest comparison and compatibility diagnostics.

The normalizer intentionally produces a small, stable interchange document.  A
consumer that caches manifests or compares adapter versions should not have to
diff arbitrary dictionaries itself.  This module compares the canonical
``Manifest`` model without exposing inline media and without treating a change
in JSON key order as a semantic change.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from payload_palette.models import Manifest


@dataclass(frozen=True, slots=True)
class ManifestDifference:
    """A bounded, machine-readable comparison of two canonical manifests."""

    before_fingerprint: str
    after_fingerprint: str
    before_part_count: int
    after_part_count: int
    changed_paths: tuple[str, ...]
    added_ordinals: tuple[int, ...]
    removed_ordinals: tuple[int, ...]
    reordered: bool

    @property
    def identical(self) -> bool:
        return not self.changed_paths and not self.added_ordinals and not self.removed_ordinals

    @property
    def append_compatible(self) -> bool:
        """Whether ``after`` preserves every old part in its original ordinal."""

        return (
            not self.removed_ordinals
            and not self.reordered
            and not self.changed_paths
            and self.added_ordinals == tuple(range(self.before_part_count, self.after_part_count))
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "before_fingerprint": self.before_fingerprint,
            "after_fingerprint": self.after_fingerprint,
            "before_part_count": self.before_part_count,
            "after_part_count": self.after_part_count,
            "identical": self.identical,
            "append_compatible": self.append_compatible,
            "reordered": self.reordered,
            "changed_paths": list(self.changed_paths),
            "added_ordinals": list(self.added_ordinals),
            "removed_ordinals": list(self.removed_ordinals),
        }


def compare_manifests(before: Manifest, after: Manifest) -> ManifestDifference:
    """Compare two manifests using semantic fields and stable ordinal paths.

    The result is deliberately bounded by the number of parts.  Fingerprints
    are compared first, but a changed fingerprint never hides the individual
    paths that changed.  ``append_compatible`` is useful for caches: it is true
    only when all shared ordinals have the same content and new parts were
    appended, not inserted or reordered.
    """

    if not isinstance(before, Manifest) or not isinstance(after, Manifest):
        raise TypeError("before and after must be Manifest instances")
    before_parts = {part.ordinal: part.to_dict() for part in before.parts}
    after_parts = {part.ordinal: part.to_dict() for part in after.parts}
    added = tuple(sorted(set(after_parts) - set(before_parts)))
    removed = tuple(sorted(set(before_parts) - set(after_parts)))
    changed: list[str] = []
    for ordinal in sorted(set(before_parts) & set(after_parts)):
        left = before_parts[ordinal]
        right = after_parts[ordinal]
        for key in sorted(set(left) | set(right)):
            if left.get(key) != right.get(key):
                changed.append(f"parts[{ordinal}].{key}")
    before_fingerprints = tuple(part.fingerprint for part in before.parts)
    after_fingerprints = tuple(part.fingerprint for part in after.parts)
    reordered = (
        len(before_fingerprints) == len(after_fingerprints)
        and sorted(before_fingerprints) == sorted(after_fingerprints)
        and before_fingerprints != after_fingerprints
    )
    if before.schema_version != after.schema_version:
        changed.append("schema_version")
    return ManifestDifference(
        before_fingerprint=before.fingerprint,
        after_fingerprint=after.fingerprint,
        before_part_count=len(before.parts),
        after_part_count=len(after.parts),
        changed_paths=tuple(changed),
        added_ordinals=added,
        removed_ordinals=removed,
        reordered=reordered,
    )


__all__ = ["ManifestDifference", "compare_manifests"]
