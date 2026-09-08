"""Offline exact-type construction, isolated defaults, and strict JSON round-trip."""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from payload_palette import DataclassAdapter, FieldConstraints, PayloadValidationError


@dataclass(frozen=True, slots=True)
class Detection:
    label: Literal["bicycle", "person"]
    score: Annotated[float, FieldConstraints(minimum=0, maximum=1)]


@dataclass(kw_only=True)
class Result:
    source: str
    detections: list[Detection]
    tags: list[str] = field(default_factory=list)


def main() -> None:
    adapter = DataclassAdapter(Result)
    result = adapter.validate_json_bytes(
        b'{"source":"offline-fixture","detections":[{"label":"bicycle","score":0.8}]}'
    )
    assert type(result.detections[0]) is Detection
    document = adapter.dump_python(result)
    result.tags.append("reviewed")
    assert isinstance(document, dict) and document["tags"] == []
    encoded = adapter.dump_json(result)
    assert adapter.validate_json_bytes(encoded) == result
    try:
        adapter.validate_python(
            {"source": "offline", "detections": [{"label": "bicycle", "score": 2}]}
        )
    except PayloadValidationError as error:
        assert error.issues[0].path == '$["detections"][0]["score"]'
    else:
        raise AssertionError("out-of-range confidence unexpectedly accepted")
    print(encoded.decode("utf-8"))


if __name__ == "__main__":
    main()
