"""Model invocation boundary.

The v0.3 implementation adapts completion-style providers and emits one text delta. A
future streaming provider/retry middleware can be implemented here without changing the
Session/Turn/Step loop.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from traceh.api.llm import LlmProvider, ModelRequest, ModelResponse

TextDeltaHandler = Callable[[str], Awaitable[None]]


class LlmRuntime:
    async def invoke(
        self,
        provider: LlmProvider,
        request: ModelRequest,
        *,
        on_text_delta: TextDeltaHandler | None = None,
    ) -> ModelResponse:
        response = await provider.complete(request)
        if response.content and on_text_delta is not None:
            await on_text_delta(response.content)
        return response
