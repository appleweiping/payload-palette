from __future__ import annotations

from payload_palette import compare_manifests, normalize


def test_compare_manifests_reports_semantic_changes() -> None:
    before = normalize([{"type": "text", "text": "hello"}])
    after = normalize(
        [
            {"type": "text", "text": "hello"},
            {"type": "text", "text": "world"},
        ]
    )
    difference = compare_manifests(before, after)
    assert difference.identical is False
    assert difference.append_compatible is True
    assert difference.added_ordinals == (1,)
    assert difference.removed_ordinals == ()
    assert difference.changed_paths == ()
    assert difference.to_dict()["after_part_count"] == 2


def test_compare_manifests_detects_reorder_and_content_change() -> None:
    before = normalize([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}])
    after = normalize([{"type": "text", "text": "b"}, {"type": "text", "text": "a"}])
    difference = compare_manifests(before, after)
    assert difference.reordered is True
    assert "parts[0].fingerprint" in difference.changed_paths
    assert difference.append_compatible is False


def test_compare_manifests_rejects_non_models() -> None:
    try:
        compare_manifests(object(), object())  # type: ignore[arg-type]
    except TypeError as error:
        assert "Manifest" in str(error)
    else:  # pragma: no cover
        raise AssertionError("expected type error")
