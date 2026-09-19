"""Independent final-publication deadline regression tests."""

import asyncio
import json

import pytest
from tests._async_generation_helpers import Check, runner

from payload_palette import GenerationPolicy, OutputReport, RuleResult


@pytest.mark.parametrize("phase", ["accepted_output_copy", "accepted_report_encoding"])
@pytest.mark.parametrize("advance", [0, 10, 11])
def test_no_acceptance_after_final_output_materialization_exceeds_deadline(
    monkeypatch, phase, advance
):
    async def scenario():
        loop = asyncio.get_running_loop()
        now = [loop.time()]
        monkeypatch.setattr(loop, "time", lambda: now[0])
        checked, crossed = [], []

        def check(value, context):
            checked.append(value)
            return RuleResult(True)

        def consume():
            crossed.append(phase)
            now[0] += advance

        if phase == "accepted_output_copy":
            original = OutputReport.output.fget

            def output(report):
                value = original(report)
                if checked:
                    consume()
                return value

            monkeypatch.setattr(OutputReport, "output", property(output))
        else:
            original = json.dumps

            def dumps(value, **kwargs):
                result = original(value, **kwargs)
                if checked and value == "yes" and "separators" not in kwargs:
                    consume()
                return result

            monkeypatch.setattr(json, "dumps", dumps)

        instance, provider = runner(Check(check), policy=GenerationPolicy(total_timeout_seconds=10))
        report = await instance.run("offline")
        assert checked == ["yes"] and crossed == [phase]
        assert len(provider.requests) == 1 and report.validator_invocations == 1
        assert report.reported_tokens == 3 and report.response_bytes == 5 and report.usage_complete
        assert report.termination == ("deadline" if advance >= 10 else "accepted")
        assert report.attempts[0].status == ("timeout" if advance >= 10 else "accepted")
        assert report.output == (None if advance >= 10 else "yes")
        assert len(asyncio.all_tasks()) == 1

    asyncio.run(scenario())
