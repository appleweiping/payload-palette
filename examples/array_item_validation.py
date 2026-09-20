"""Offline per-item semantic validation; also works from an installed package."""

from payload_palette import ArrayRuleBinding, OutputSchema, TrimmedString, ValidationPipeline


def run_example() -> dict[str, object]:
    item = OutputSchema("object", properties={"label": OutputSchema("string")})
    schema = OutputSchema("object", properties={"predictions": OutputSchema("array", items=item)})
    pipeline = ValidationPipeline(
        schema,
        (ArrayRuleBinding("trim_labels", ("predictions",), ("label",), TrimmedString(), "fix"),),
    )
    report = pipeline.validate({"predictions": [{"label": " yes "}, {"label": "no"}]})
    expected = {"predictions": [{"label": "yes"}, {"label": "no"}]}
    if not report.valid or report.output != expected or report.invocations != 5:
        raise RuntimeError("per-item validation example did not match its contract")
    return {"output": report.output, "invocations": report.invocations}


if __name__ == "__main__":
    print(run_example())
