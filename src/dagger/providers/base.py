"""Provider contracts and registry."""

import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Awaitable, Callable
from weakref import WeakKeyDictionary

card_log_hook: ContextVar[Callable[[str, str], Awaitable[None] | None] | None] = ContextVar(
    "card_log_hook", default=None
)

if TYPE_CHECKING:
    from dagger.schema import AgentContainer, TaskEnvelope, UsageEvent


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    try:
        import tiktoken

        enc = tiktoken.get_encoding("cl100k_base")
        return max(1, len(enc.encode(text)))
    except Exception:
        return max(1, len(text) // 4)


def estimate_cost_usd(cost_tier: str, input_tokens: int, output_tokens: int) -> float:
    if cost_tier != "metered_api":
        return 0.0
    return round((input_tokens * 0.5 + output_tokens * 1.5) / 1_000_000, 6)


class ProviderError(RuntimeError):
    """A provider could not complete a task."""


class ProviderRateLimitError(ProviderError):
    """A provider reported a rate or usage limit."""


_RATE_MARKERS = ("429", "rate limit", "rate_limit", "quota exceeded", "usage limit", "too many requests")


def raise_provider_error(message: str) -> None:
    if any(marker in message.lower() for marker in _RATE_MARKERS):
        raise ProviderRateLimitError(message)
    raise ProviderError(message)


class BaseProvider(ABC):
    _limiters: WeakKeyDictionary = WeakKeyDictionary()

    def __init__(
        self,
        name: str,
        *,
        max_concurrency: int | None = None,
        usage_callback: Callable[["UsageEvent"], Awaitable[Any] | Any] | None = None,
    ) -> None:
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.name = name
        self.max_concurrency = max_concurrency
        self._usage_callback = usage_callback

    async def _emit_usage(self, event: "UsageEvent") -> None:
        if self._usage_callback is None:
            return
        try:
            res = self._usage_callback(event)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            pass

    @asynccontextmanager
    async def semaphore(self, limit: int | None = None):
        loop = asyncio.get_running_loop()
        providers = self._limiters.setdefault(loop, {})
        if self.name not in providers:
            providers[self.name] = (asyncio.Condition(), [])
        condition, active_limits = providers[self.name]
        limit = limit or self.max_concurrency or 2
        if self.max_concurrency is not None:
            limit = min(limit, self.max_concurrency)
        async with condition:
            await condition.wait_for(
                lambda: len(active_limits) < min([limit, *active_limits])
            )
            active_limits.append(limit)
        try:
            yield
        finally:
            async with condition:
                active_limits.remove(limit)
                condition.notify_all()

    @abstractmethod
    async def run(self, container: "AgentContainer", task: "TaskEnvelope") -> str:
        """Execute a task using the given agent configuration."""


class ProviderRegistry:
    def __init__(
        self,
        *,
        defaults: bool = True,
        db: Any = None,
        usage_callback: Callable[["UsageEvent"], Awaitable[Any] | Any] | None = None,
    ) -> None:
        self._providers: dict[str, BaseProvider] = {}
        if usage_callback is None and db is not None:
            usage_callback = db.record_usage_event
        self._usage_callback = usage_callback
        if defaults:
            from .api_runner import OpenAICompatibleApiRunner
            from .cli_runners import AgyCliRunner, ClaudeCliRunner, CodexCliRunner, CopilotCliRunner, GeminiCliRunner

            for name, runner in (
                ("claude", ClaudeCliRunner()),
                ("codex", CodexCliRunner()),
                ("gemini", GeminiCliRunner()),
                ("copilot", CopilotCliRunner()),
                ("agy", AgyCliRunner()),
                ("api", OpenAICompatibleApiRunner()),
                ("groq", OpenAICompatibleApiRunner()),
            ):
                self.register(name, runner)

    def register(self, name: str, provider: BaseProvider) -> None:
        if not name or not name.strip():
            raise ValueError("provider name must not be empty")
        if provider._usage_callback is None and self._usage_callback is not None:
            provider._usage_callback = self._usage_callback
        self._providers[name.lower()] = provider

    def get(self, name: str) -> BaseProvider:
        try:
            return self._providers[name.lower()]
        except KeyError:
            raise KeyError(f"Unknown provider: {name}") from None

    async def run(
        self,
        container: "AgentContainer",
        task: "TaskEnvelope",
        *,
        usage_context: dict[str, Any] | None = None,
    ) -> str:
        runner = self.get(container.provider)
        try:
            return await runner.run(container, task, usage_context=usage_context)
        except TypeError:
            return await runner.run(container, task)
