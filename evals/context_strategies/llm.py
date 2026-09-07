"""The one model call every strategy shares, and the tally it keeps.

Holding the model fixed is what makes the comparison atomic: between two cells
of the matrix the only difference is the strategy. Every call is recorded, so a
strategy that wins by spending ten times as much cannot look free.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from inference.models import CompletionRequest, CompletionResponse, PoolPriority


@dataclass
class CallRecord:
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float


@dataclass
class Ask:
    """Calls the model and tallies what it cost. One instance per strategy run."""

    router: object  # anything with async complete(CompletionRequest) -> CompletionResponse
    component: str = "eval:context_strategies"
    pool: str | None = None
    priority: PoolPriority = PoolPriority.MEDIUM
    max_tokens: int | None = None
    calls: list[CallRecord] = field(default_factory=list)

    async def __call__(self, prompt: str, *, max_tokens: int | None = None) -> str:
        response = await self.router.complete(
            CompletionRequest(
                prompt=prompt,
                component=self.component,
                pool=self.pool,
                priority=self.priority,
                max_tokens=max_tokens or self.max_tokens,
            )
        )
        self.calls.append(
            CallRecord(
                model=response.model_used,
                tokens_in=response.tokens_in,
                tokens_out=response.tokens_out,
                cost_usd=response.cost_usd,
            )
        )
        return response.text or ""

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def tokens_in(self) -> int:
        return sum(call.tokens_in for call in self.calls)

    @property
    def tokens_out(self) -> int:
        return sum(call.tokens_out for call in self.calls)

    @property
    def cost_usd(self) -> float:
        return sum(call.cost_usd for call in self.calls)

    @property
    def model(self) -> str:
        """The model that answered, or "" before the first call."""
        return self.calls[0].model if self.calls else ""


class ScriptedRouter:
    """A stand-in model for --dry-run: proves the plumbing without spending.

    It answers whatever task it is given correctly, by whichever route the
    strategy uses - a direct reply, or code that leaves the answer in the REPL.
    So a dry run that does not score 1.00 everywhere means the harness is broken,
    not that the model is weak.
    """

    def __init__(self, gold_reply: str) -> None:
        self._gold = gold_reply
        self.prompts: list[str] = []

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        self.prompts.append(request.prompt)
        text = self._reply(request.prompt)
        return CompletionResponse(
            text=text,
            model_used="scripted/dry-run",
            tokens_in=len(request.prompt) // 4,
            tokens_out=len(text) // 4,
            cost_usd=0.0,
        )

    def _reply(self, prompt: str) -> str:
        if "```repl" not in prompt:
            return self._gold
        # A REPL turn: leave the answer in the namespace, as a real trajectory would.
        return f"```repl\nprint(len(context))\nFINAL_ANSWER = {self._gold!r}\n```"


class PinnedRouter:
    """One provider, one model id - the bench's fixed point.

    north's dispatcher is free to route each call to whichever model is cheapest
    and healthiest, which is right in production and fatal here: the first run of
    this bench answered some cells with minimax and others with qwen, so the
    numbers compared two models rather than two strategies. Pinning removes that.
    """

    def __init__(self, provider: object, model_id: str) -> None:
        self._provider = provider
        self._model_id = model_id

    async def complete(self, request: CompletionRequest) -> CompletionResponse:
        return await self._provider.complete(self._model_id, request)


def pinned_router(spec: str) -> PinnedRouter:
    """Build a router pinned to one model, from a "provider:model_id" spec."""
    from config.settings import settings

    provider_id, _, model_id = spec.partition(":")
    if not model_id:
        raise SystemExit(f"--model wants provider:model_id, e.g. openrouter:qwen/qwen3-30b (got {spec!r})")

    if provider_id == "codex":
        from inference.codex_auth import CodexCredentialProvider
        from inference.providers.openai_codex import OpenAICodexProvider

        provider = OpenAICodexProvider(CodexCredentialProvider())
    elif provider_id == "openrouter":
        from inference.providers.openrouter import OpenRouterRouter

        provider = OpenRouterRouter(api_key=settings.openrouter_api_key)
    elif provider_id == "groq":
        from inference.providers.groq import GroqRouter

        provider = GroqRouter(api_key=settings.groq_api_key)
    elif provider_id == "gemini":
        from inference.providers.gemini import GeminiRouter

        provider = GeminiRouter(api_key=settings.gemini_api_key)
    else:
        raise SystemExit(f"unknown provider {provider_id!r}. Known: codex, openrouter, groq, gemini")
    return PinnedRouter(provider, model_id)
