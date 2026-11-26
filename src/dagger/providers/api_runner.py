"""OpenAI-compatible HTTP execution."""

import os
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from dagger.schema import UsageEvent
from dagger.vault import get_vault_credential

from .base import (
    BaseProvider,
    ProviderError,
    ProviderRateLimitError,
    _RATE_MARKERS,
    estimate_cost_usd,
    estimate_tokens,
)


def _raise_api_error(message: str, detail: str = "") -> None:
    # Upstream errors can echo credentials or URLs, so use them only to classify.
    if any(marker in (message + " " + detail).lower() for marker in _RATE_MARKERS):
        raise ProviderRateLimitError(message) from None
    raise ProviderError(message) from None


class OpenAICompatibleApiRunner(BaseProvider):
    def __init__(self, *, timeout: float = 120.0, max_concurrency: int | None = None) -> None:
        super().__init__("api", max_concurrency=max_concurrency)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout

    async def run(
        self, container, task, *, usage_context: dict[str, Any] | None = None
    ) -> str:
        is_groq = container.provider == "groq" or (
            container.base_url and "api.groq.com" in container.base_url
        )
        is_local = getattr(container, "cost_tier", "metered_api") == "local_free"
        if is_groq:
            base_url = container.base_url or "https://api.groq.com/openai/v1"
        elif is_local:
            base_url = container.base_url or "http://127.0.0.1:8080/v1"
        else:
            base_url = (
                container.base_url
                or get_vault_credential("DAGGER_API_BASE_URL")
                or "http://127.0.0.1:8080/v1"
            )
        try:
            parsed_url = urlsplit(base_url)
            if (
                parsed_url.scheme not in ("http", "https")
                or not parsed_url.hostname
                or parsed_url.username is not None
                or parsed_url.password is not None
            ):
                raise ValueError
        except ValueError:
            raise ProviderError("API base URL must be an HTTP(S) URL without embedded credentials") from None
        if not container.model:
            raise ProviderError("API model is not configured")
        headers = {}
        if is_groq:
            api_key = (
                (os.getenv(container.api_key_env) if container.api_key_env else None)
                or get_vault_credential("GROQ_API_KEY")
            )
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
        elif container.provider == "api":
            if container.api_key_env:
                api_key = os.getenv(container.api_key_env)
            elif not is_local:
                api_key = get_vault_credential("DAGGER_API_KEY")
            else:
                api_key = None
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
        messages = []
        if container.system_prompt:
            messages.append({"role": "system", "content": container.system_prompt})
        messages.append({"role": "user", "content": task.prompt})
        started = time.monotonic()
        success = False
        result_text = ""
        in_tok = estimate_tokens(
            (container.system_prompt + "\n" if container.system_prompt else "") + task.prompt
        )
        out_tok = 0
        ctx = usage_context or {}
        try:
            async with self.semaphore(container.max_concurrency):
                try:
                    async with httpx.AsyncClient(timeout=self.timeout) as client:
                        response = await client.post(
                            f"{base_url.rstrip('/')}/chat/completions",
                            headers=headers,
                            json={"model": container.model, "messages": messages},
                        )
                    if response.is_error:
                        _raise_api_error(f"API provider returned HTTP {response.status_code}", response.text)
                    payload = response.json()
                    if payload.get("error"):
                        _raise_api_error("API provider returned an error", str(payload["error"]))
                    usage_obj = payload.get("usage")
                    if isinstance(usage_obj, dict):
                        if isinstance(usage_obj.get("prompt_tokens"), int):
                            in_tok = usage_obj["prompt_tokens"]
                        if isinstance(usage_obj.get("completion_tokens"), int):
                            out_tok = usage_obj["completion_tokens"]
                    result = payload["choices"][0]["message"]["content"]
                    if not isinstance(result, str):
                        raise ValueError("response content is not text")
                    result_text = result
                    if out_tok == 0:
                        out_tok = estimate_tokens(result_text)
                    success = True
                    return result_text
                except httpx.HTTPError as exc:
                    _raise_api_error(f"API provider request failed ({type(exc).__name__})", str(exc))
                except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                    raise ProviderError("Invalid API provider response") from None
        finally:
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            cost_tier = ctx.get("cost_tier") or getattr(container, "cost_tier", "metered_api")
            await self._emit_usage(
                UsageEvent(
                    run_id=ctx.get("run_id", getattr(task, "run_id", None)),
                    kanban_id=ctx.get("kanban_id", getattr(task, "kanban_id", None)),
                    container_id=ctx.get("container_id", getattr(container, "container_id", None)),
                    provider="groq" if is_groq else container.provider,
                    model=container.model,
                    cost_tier=cost_tier,
                    duration_ms=duration_ms,
                    input_tokens_est=in_tok,
                    output_tokens_est=out_tok,
                    estimated_cost_usd=estimate_cost_usd(cost_tier, in_tok, out_tok),
                    success=success,
                )
            )
