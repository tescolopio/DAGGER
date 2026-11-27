"""Execution providers for subscription CLIs and OpenAI-compatible APIs."""

from .api_runner import OpenAICompatibleApiRunner
from .base import BaseProvider, ProviderError, ProviderRateLimitError, ProviderRegistry
from .cli_runners import AgyCliRunner, ClaudeCliRunner, CodexCliRunner, CopilotCliRunner, GeminiCliRunner
from .discovery import (
    SERVICE_PROVIDER_BY_ID,
    SERVICE_PROVIDER_METADATA,
    DiscoveryResult,
    discover_provider_models,
    has_copilot_local_auth,
    has_gemini_subscription_auth,
)

__all__ = [
    "BaseProvider",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderRegistry",
    "AgyCliRunner",
    "ClaudeCliRunner",
    "CodexCliRunner",
    "GeminiCliRunner",
    "CopilotCliRunner",
    "OpenAICompatibleApiRunner",
    "DiscoveryResult",
    "SERVICE_PROVIDER_METADATA",
    "SERVICE_PROVIDER_BY_ID",
    "discover_provider_models",
    "has_copilot_local_auth",
    "has_gemini_subscription_auth",
]
