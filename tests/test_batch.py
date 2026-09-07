from __future__ import annotations

from payload_palette import manifest_schema, normalize_batch


def test_batch_keeps_valid_and_invalid_records() -> None:
    report = normalize_batch(
        [
            [{"type": "text", "text": "hello"}],
            [{"type": "image", "data": "not-base64", "mime_type": "image/png"}],
        ]
    )
    assert report.valid_count == 1
    assert report.invalid_count == 1
    assert report.records[0].manifest is not None
    assert report.records[1].errors[0]["path"]
    assert len(report.input_sha256) == 64


def test_manifest_schema_is_versioned() -> None:
    schema = manifest_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["schema_version"]["const"] == "1.0"
