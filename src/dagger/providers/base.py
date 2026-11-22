"""Provider contracts and registry."""

import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

if TYPE_CHECKING:
    from dagger.schema import AgentContainer, TaskEnvelope


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

    def __init__(self, name: str, *, max_concurrency: int | None = None) -> None:
        if max_concurrency is not None and max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        self.name = name
        self.max_concurrency = max_concurrency

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
    def __init__(self, *, defaults: bool = True) -> None:
        self._providers: dict[str, BaseProvider] = {}
        if defaults:
            from .api_runner import OpenAICompatibleApiRunner
            from .cli_runners import ClaudeCliRunner, CodexCliRunner, CopilotCliRunner, GeminiCliRunner

            for name, runner in (
                ("claude", ClaudeCliRunner()),
                ("codex", CodexCliRunner()),
                ("gemini", GeminiCliRunner()),
                ("copilot", CopilotCliRunner()),
                ("api", OpenAICompatibleApiRunner()),
            ):
                self.register(name, runner)

    def register(self, name: str, provider: BaseProvider) -> None:
        if not name or not name.strip():
            raise ValueError("provider name must not be empty")
        self._providers[name.lower()] = provider

    def get(self, name: str) -> BaseProvider:
        try:
            return self._providers[name.lower()]
        except KeyError:
            raise KeyError(f"Unknown provider: {name}") from None
