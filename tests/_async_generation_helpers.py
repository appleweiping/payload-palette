"""Offline adapters for semantic generation tests; no SDK, I/O or credentials."""

import asyncio

from payload_palette import (
    AsyncGenerationRunner,
    AsyncRuleBinding,
    AsyncValidationPipeline,
    AsyncValidationPolicy,
    GeneratedResponse,
    GenerationPolicy,
    OutputLimits,
    OutputSchema,
    RuleResult,
    TokenUsage,
    ValidationPipeline,
)


class Provider:
    def __init__(self, *responses):
        self.responses, self.requests = responses, []

    async def generate(self, request):
        self.requests.append(request)
        await asyncio.sleep(0)
        response = self.responses[len(self.requests) - 1]
        return GeneratedResponse(response, TokenUsage(2, 1))


class Check:
    def __init__(self, function=None):
        self.function = function

    async def check(self, value, context):
        await asyncio.sleep(0)
        return RuleResult(True) if self.function is None else self.function(value, context)


class AwaitCheck:
    def __init__(self, function):
        self.function = function

    async def check(self, value, context):
        return await self.function(value, context)


def runner(
    *rules, responses=('"yes"',), sync=(), schema=None, limits=None, async_policy=None, policy=None
):
    provider = Provider(*responses)
    pipeline = AsyncValidationPipeline(
        ValidationPipeline(schema or OutputSchema("string"), sync, limits or OutputLimits()),
        tuple(AsyncRuleBinding(f"a{i}", (), rule) for i, rule in enumerate(rules)),
        async_policy or AsyncValidationPolicy(),
    )
    return AsyncGenerationRunner(pipeline, provider, policy or GenerationPolicy()), provider
