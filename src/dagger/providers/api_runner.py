"""OpenAI-compatible HTTP execution."""

import os
from urllib.parse import urlsplit

import httpx

from .base import BaseProvider, ProviderError, ProviderRateLimitError, _RATE_MARKERS


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

    async def run(self, container, task) -> str:
        base_url = container.base_url or "http://127.0.0.1:8080/v1"
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
        if container.provider == "api" and container.api_key_env:
            api_key = os.getenv(container.api_key_env)
            if api_key:
                headers["Authorization"] = "Bearer " + api_key
        messages = []
        if container.system_prompt:
            messages.append({"role": "system", "content": container.system_prompt})
        messages.append({"role": "user", "content": task.prompt})
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
                result = payload["choices"][0]["message"]["content"]
                if not isinstance(result, str):
                    raise ValueError("response content is not text")
                return result
            except httpx.HTTPError as exc:
                _raise_api_error(f"API provider request failed ({type(exc).__name__})", str(exc))
            except (ValueError, KeyError, IndexError, TypeError, AttributeError):
                raise ProviderError("Invalid API provider response") from None
