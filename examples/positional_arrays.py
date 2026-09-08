"""Offline schema exchange, exact-position repair and typed tuple reconstruction."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from payload_palette import (
    AnnotationAdapter,
    DataclassAdapter,
    RuleBinding,
    TrimmedString,
    ValidationPipeline,
    export_output_schema,
    load_output_schema,
)


@dataclass(frozen=True, slots=True)
class Observation:
    sample: tuple[int, str]
    calibration: tuple[date, Decimal]
    flags: tuple[bool, ...] = ()


def main() -> None:
    pair = AnnotationAdapter(tuple[int, str])
    schema = load_output_schema(export_output_schema(pair.schema))
    pipeline = ValidationPipeline(schema, (RuleBinding("trim", (1,), TrimmedString(), "fix"),))
    repaired = pipeline.validate([7, " camera "])
    assert repaired.valid and repaired.output == [7, "camera"]
    adapter = DataclassAdapter(Observation)
    result = adapter.validate_python(
        {"sample": repaired.output, "calibration": ["2024-02-29", "1.250"]}
    )
    assert result.sample == (7, "camera") and type(result.flags) is tuple
    assert result.calibration == (date(2024, 2, 29), Decimal("1.250"))
    assert adapter.validate_json_bytes(adapter.dump_json(result)) == result
    print(adapter.dump_json(result).decode())


if __name__ == "__main__":
    main()
