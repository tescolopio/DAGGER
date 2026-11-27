"""Non-destructive provider readiness and available-model discovery."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import Field, field_validator

from dagger.providers.cli_runners import AgyCliRunner
from dagger.schema import AvailableModel, DiscoveryStatusValue, StrictModel
from dagger.vault import get_base_url, get_vault_credential, validate_base_url

DiscoveryProviderStatus = Literal[
    "authenticated",
    "configured",
    "unauthenticated",
    "expired",
    "missing_binary",
    "unreachable",
    "error",
]

DEFAULT_GROQ_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_LOCAL_BASE_URL = "http://127.0.0.1:8080/v1"

CLI_PROBE_STRIPPED_ENV: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "GROQ_API_KEY",
    "TAVILY_API_KEY",
    "DAGGER_API_KEY",
    "DAGGER_API_BASE_URL",
    "DAGGER_LOCAL_API_KEY",
    "DAGGER_LOCAL_BASE_URL",
    "COPILOT_GITHUB_TOKEN",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_BASE_URL",
)

SERVICE_PROVIDER_METADATA: tuple[dict[str, Any], ...] = (
    {
        "provider_id": "claude",
        "display_name": "Claude Code CLI",
        "kind": "subscription_cli",
        "category": "llm",
        "binary": "claude",
        "supports_login": True,
        "supports_api_key": False,
        "supports_base_url": False,
        "requires_api_key": False,
    },
    {
        "provider_id": "codex",
        "display_name": "Codex CLI",
        "kind": "subscription_cli",
        "category": "llm",
        "binary": "codex",
        "supports_login": True,
        "supports_api_key": False,
        "supports_base_url": False,
        "requires_api_key": False,
    },
    {
        "provider_id": "gemini",
        "display_name": "Gemini CLI",
        "kind": "subscription_cli",
        "category": "llm",
        "binary": "gemini",
        "supports_login": True,
        "supports_api_key": False,
        "supports_base_url": False,
        "requires_api_key": False,
    },
    {
        "provider_id": "copilot",
        "display_name": "GitHub Copilot CLI",
        "kind": "subscription_cli",
        "category": "llm",
        "binary": "copilot",
        "supports_login": True,
        "supports_api_key": False,
        "supports_base_url": False,
        "requires_api_key": False,
    },
    {
        "provider_id": "agy",
        "display_name": "Antigravity CLI",
        "kind": "subscription_cli",
        "category": "llm",
        "binary": "agy",
        "supports_login": True,
        "supports_api_key": False,
        "supports_base_url": False,
        "requires_api_key": False,
    },
    {
        "provider_id": "groq",
        "display_name": "Groq API",
        "kind": "api_key",
        "category": "llm",
        "env_key": "GROQ_API_KEY",
        "default_base_url": DEFAULT_GROQ_BASE_URL,
        "supports_login": False,
        "supports_api_key": True,
        "supports_base_url": False,
        "requires_api_key": True,
    },
    {
        "provider_id": "api",
        "display_name": "Custom OpenAI-Compatible API",
        "kind": "api_key",
        "category": "llm",
        "env_key": "DAGGER_API_KEY",
        "base_url_env_key": "DAGGER_API_BASE_URL",
        "supports_login": False,
        "supports_api_key": True,
        "supports_base_url": True,
        "requires_api_key": True,
    },
    {
        "provider_id": "tavily",
        "display_name": "Tavily Search API",
        "kind": "api_key",
        "category": "tool",
        "env_key": "TAVILY_API_KEY",
        "supports_login": False,
        "supports_api_key": True,
        "supports_base_url": False,
        "requires_api_key": True,
    },
    {
        "provider_id": "local",
        "display_name": "Local OpenAI-Compatible",
        "kind": "local",
        "category": "local",
        "env_key": "DAGGER_LOCAL_API_KEY",
        "base_url_env_key": "DAGGER_LOCAL_BASE_URL",
        "default_base_url": DEFAULT_LOCAL_BASE_URL,
        "supports_login": False,
        "supports_api_key": False,
        "supports_base_url": True,
        "requires_api_key": False,
    },
)

SERVICE_PROVIDER_BY_ID: dict[str, dict[str, Any]] = {
    item["provider_id"]: item for item in SERVICE_PROVIDER_METADATA
}

_CLAUDE_CURATED_MODELS: tuple[tuple[str, str, int], ...] = (
    ("claude-sonnet-4-5", "Claude Sonnet 4.5", 200_000),
    ("claude-opus-4-5", "Claude Opus 4.5", 200_000),
    ("claude-haiku-4-5", "Claude Haiku 4.5", 200_000),
)

_CODEX_CURATED_MODELS: tuple[tuple[str, str, int], ...] = (
    ("gpt-5-codex", "GPT-5 Codex", 128_000),
    ("gpt-5.2-codex", "GPT-5.2 Codex", 128_000),
    ("gpt-5.3-codex", "GPT-5.3 Codex", 128_000),
    ("gpt-4.1", "GPT-4.1", 128_000),
    ("gpt-4.1-mini", "GPT-4.1 Mini", 128_000),
)

_GEMINI_FALLBACK_MODELS: tuple[tuple[str, str, int], ...] = (
    ("gemini-2.5-pro", "gemini-2.5-pro", 1_048_576),
    ("gemini-3.5-flash", "gemini-3.5-flash", 1_048_576),
    ("gemini-3.1-flash-lite", "gemini-3.1-flash-lite", 1_048_576),
)

_COPILOT_FALLBACK_MODELS: tuple[tuple[str, str, int], ...] = (
    ("gpt-4.1", "gpt-4.1", 128_000),
    ("gpt-5.2", "gpt-5.2", 128_000),
    ("claude-sonnet-4.5", "claude-sonnet-4.5", 200_000),
    ("gpt-5-mini", "gpt-5-mini", 128_000),
)

_GROQ_FALLBACK_MODELS: tuple[tuple[str, str, int], ...] = (
    ("llama-3.3-70b-versatile", "llama-3.3-70b-versatile", 131_072),
    ("llama-3.1-8b-instant", "llama-3.1-8b-instant", 131_072),
)

_NON_LLM_MODEL_MARKERS: tuple[str, ...] = (
    "whisper",
    "tts",
    "playai-tts",
    "orpheus",
    "speech",
    "transcrib",
    "embedding",
    "rerank",
    "prompt-guard",
    "safeguard",
    "moderation",
    "audio",
)

_AUTH_DENIED_MARKERS: tuple[str, ...] = (
    "not logged in",
    "not authenticated",
    "unauthenticated",
    "logged out",
    "login required",
    "sign in",
    "unauthorized",
    "please set an auth method",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DiscoveryResult(StrictModel):
    provider_id: str = Field(min_length=1)
    status: DiscoveryProviderStatus
    detail: str = ""
    models: list[AvailableModel] = Field(default_factory=list)
    discovery_status: DiscoveryStatusValue = "unverified"
    discovery_error: str | None = None
    version: str | None = None
    base_url: str | None = None
    checked_at: datetime = Field(default_factory=_utc_now)

    @field_validator("checked_at", mode="before")
    @classmethod
    def parse_checked_at(cls, value: Any) -> Any:
        return datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value


def _sanitized_cli_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in CLI_PROBE_STRIPPED_ENV:
        env.pop(key, None)
    return env


async def _run_cli_probe(
    *args: str, timeout: float = 5.0
) -> tuple[bool, int | None, str, str]:
    """Execute a non-destructive CLI probe with sanitized env and bounded timeout."""
    env = _sanitized_cli_env()
    try:
        process = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except OSError as exc:
        return False, None, "", str(exc)
    try:
        stdout_b, stderr_b = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        return False, None, "", f"command timed out after {timeout:g}s"
    stdout = (stdout_b or b"").decode("utf-8", errors="replace").strip()
    stderr = (stderr_b or b"").decode("utf-8", errors="replace").strip()
    return process.returncode == 0, process.returncode, stdout, stderr


def is_auth_output_healthy(auth_ok: bool, output: str) -> bool:
    """Interpret CLI auth status stdout/stderr without relying on exit code alone."""
    normalized = output.lower()
    try:
        details = json.loads(output)
    except (ValueError, TypeError):
        details = None
    denied = isinstance(details, dict) and any(
        details.get(field) is False for field in ("loggedIn", "logged_in", "authenticated")
    )
    return (
        auth_ok
        and not any(marker in normalized for marker in _AUTH_DENIED_MARKERS)
        and not denied
    )


def has_gemini_subscription_auth() -> bool:
    """Return True only when Gemini CLI has subscription OAuth/GCA/Vertex auth.

    Never treats metered GEMINI_API_KEY / GOOGLE_API_KEY as valid Gemini CLI auth
    because GeminiCliRunner strips metered API keys from its subprocess environment.
    """
    for flag in ("GOOGLE_GENAI_USE_GCA", "GOOGLE_GENAI_USE_VERTEXAI"):
        val = os.environ.get(flag, "").strip().lower()
        if val and val not in {"false", "0", "no"}:
            return True
    gemini_dir = Path.home() / ".gemini"
    settings_path = gemini_dir / "settings.json"
    if not settings_path.is_file():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(settings, dict):
        return False
    security = settings.get("security")
    auth = security.get("auth") if isinstance(security, dict) else None
    selected = settings.get("selectedType") or (
        auth.get("selectedType") if isinstance(auth, dict) else None
    )
    if not isinstance(selected, str) or not selected.strip():
        return False
    selected_norm = selected.strip().lower()
    if selected_norm in {"gemini-api-key", "api-key", "api_key"}:
        return False
    if selected_norm == "oauth-personal":
        return (gemini_dir / "oauth_creds.json").is_file()
    return True


def has_copilot_local_auth() -> bool:
    """Return True when ~/.copilot contains non-empty stored login metadata/tokens."""
    copilot_dir = Path.home() / ".copilot"
    if not copilot_dir.is_dir():
        return False
    for filename in ("hosts.json", "apps.json", "config.json"):
        candidate = copilot_dir / filename
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if filename in ("hosts.json", "apps.json") and any(
            isinstance(v, dict) and bool(v.get("oauth_token")) for v in payload.values()
        ):
            return True
        if filename == "config.json":
            if payload.get("logged_in_users") or payload.get("oauth_token") or payload.get("last_logged_in_user"):
                return True
    return False


def _is_groq_llm_model(model_id: str) -> bool:
    lowered = model_id.strip().lower()
    return not any(marker in lowered for marker in _NON_LLM_MODEL_MARKERS)


def parse_agy_models_output(
    output: str, *, checked_at: datetime | None = None
) -> list[AvailableModel]:
    """Parse `agy models` stdout into deduplicated AvailableModel entries."""
    now = checked_at or _utc_now()
    known_by_slug = {slug.lower(): (slug, label) for slug, label in AgyCliRunner.SUPPORTED_MODEL_SPECS}
    known_by_label = {label.lower(): (slug, label) for slug, label in AgyCliRunner.SUPPORTED_MODEL_SPECS}
    models: list[AvailableModel] = []
    seen: set[str] = set()

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(("I", "E", "W")) and re.match(r"^[IEW]\d{4}\s", line):
            continue
        lower = line.lower()
        if lower.startswith(("available models", "model", "slug", "---", "===", "error:", "failed to", "usage:")):
            continue
        cleaned = re.sub(r"^[\-\*\u2022]+\s*", "", line).strip()
        if not cleaned:
            continue

        # Exact display label line (e.g. "Gemini 4 Neon (High)")
        if cleaned.lower() in known_by_label:
            slug, label = known_by_label[cleaned.lower()]
            if slug not in seen:
                seen.add(slug)
                models.append(
                    AvailableModel(
                        provider_id="agy",
                        model_id=slug,
                        display_name=label,
                        available=True,
                        is_llm=True,
                        context_window=1_000_000,
                        discovery_source="live",
                        last_checked=now,
                    )
                )
            continue

        # Tabular / multi-space / pipe / colon separated: "<slug>  <Display Name>"
        parts = re.split(r"\s{2,}|\t|\s+\|\s+", cleaned, maxsplit=1)
        first = parts[0].strip().strip("`\"'")
        rest = parts[1].strip() if len(parts) > 1 else ""
        if first.lower() in known_by_slug:
            slug, default_label = known_by_slug[first.lower()]
            label = rest or default_label
            if slug not in seen:
                seen.add(slug)
                models.append(
                    AvailableModel(
                        provider_id="agy",
                        model_id=slug,
                        display_name=label,
                        available=True,
                        is_llm=True,
                        context_window=1_000_000,
                        discovery_source="live",
                        last_checked=now,
                    )
                )
            continue

        # General slug format: "<slug>  <Label>" or standalone slug containing a hyphen
        if re.fullmatch(r"[a-z0-9][a-z0-9._-]*-[a-z0-9._-]+", first):
            if first not in seen:
                seen.add(first)
                models.append(
                    AvailableModel(
                        provider_id="agy",
                        model_id=first,
                        display_name=rest or first,
                        available=True,
                        is_llm=True,
                        context_window=1_000_000,
                        discovery_source="live",
                        last_checked=now,
                    )
                )

    return models


def parse_gemini_models_json(
    output: str, *, checked_at: datetime | None = None
) -> list[AvailableModel]:
    """Parse `gemini models -o json` stdout into AvailableModel entries."""
    now = checked_at or _utc_now()
    text = output.strip()
    if not text:
        return []
    start = text.find("[")
    if start == -1:
        return []
    try:
        payload = json.loads(text[start:])
    except ValueError:
        return []
    if not isinstance(payload, list):
        return []

    models: list[AvailableModel] = []
    seen: set[str] = set()
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id") or item.get("modelId")
        if not isinstance(raw_id, str) or not raw_id.strip():
            continue
        model_id = raw_id.strip()
        if model_id in seen:
            continue
        seen.add(model_id)
        display_name = item.get("displayName") or item.get("name") or model_id
        ctx = item.get("contextLimits")
        context_window = 0
        if isinstance(ctx, dict) and isinstance(ctx.get("inputTokens"), int) and ctx["inputTokens"] > 0:
            context_window = ctx["inputTokens"]
        models.append(
            AvailableModel(
                provider_id="gemini",
                model_id=model_id,
                display_name=str(display_name).strip() or model_id,
                available=True,
                is_llm=True,
                context_window=context_window,
                discovery_source="live",
                last_checked=now,
            )
        )
    return models


def parse_gemini_models_text(
    output: str, *, checked_at: datetime | None = None
) -> list[AvailableModel]:
    """Parse `gemini --list-models` text output into AvailableModel entries."""
    now = checked_at or _utc_now()
    models: list[AvailableModel] = []
    seen: set[str] = set()
    current_idx: int | None = None

    for raw_line in output.splitlines():
        if not raw_line.strip():
            current_idx = None
            continue
        if not raw_line.startswith((" ", "\t")):
            match = re.match(r"^([a-zA-Z0-9][a-zA-Z0-9._-]*)\s+\(([^)]+)\)\s*$", raw_line.strip())
            if match:
                model_id = match.group(1).strip()
                display_name = match.group(2).strip() or model_id
                if model_id not in seen:
                    seen.add(model_id)
                    models.append(
                        AvailableModel(
                            provider_id="gemini",
                            model_id=model_id,
                            display_name=display_name,
                            available=True,
                            is_llm=True,
                            context_window=0,
                            discovery_source="live",
                            last_checked=now,
                        )
                    )
                    current_idx = len(models) - 1
                else:
                    current_idx = None
            else:
                current_idx = None
        elif current_idx is not None:
            ctx_match = re.search(r"Context limit:\s*([\d,]+)\s+input tokens", raw_line, re.IGNORECASE)
            if ctx_match:
                tokens = int(ctx_match.group(1).replace(",", ""))
                models[current_idx] = models[current_idx].model_copy(
                    update={"context_window": tokens}
                )
    return models


def parse_copilot_help_config_models(
    output: str, *, checked_at: datetime | None = None
) -> list[AvailableModel]:
    """Parse the `model` bullet list from `copilot help config`."""
    now = checked_at or _utc_now()
    models: list[AvailableModel] = []
    seen: set[str] = set()
    in_model_section = False

    for raw_line in output.splitlines():
        stripped = raw_line.strip()
        if not in_model_section:
            if stripped.startswith("`model`:") or stripped.startswith("model:"):
                in_model_section = True
            continue
        if not stripped:
            if models:
                break
            continue
        if stripped.startswith("`") and ":" in stripped:
            break
        bullet = re.match(r'^-\s*"([^"]+)"', stripped) or re.match(r"^-\s*'([^']+)'", stripped)
        if bullet:
            model_id = bullet.group(1).strip()
            if model_id and model_id not in seen:
                seen.add(model_id)
                models.append(
                    AvailableModel(
                        provider_id="copilot",
                        model_id=model_id,
                        display_name=model_id,
                        available=True,
                        is_llm=True,
                        context_window=0,
                        discovery_source="live",
                        last_checked=now,
                    )
                )
    return models


def _build_static_models(
    provider_id: str,
    specs: tuple[tuple[str, str, int], ...],
    *,
    available: bool,
    discovery_source: Literal["curated", "fallback", "static"],
    checked_at: datetime,
) -> list[AvailableModel]:
    return [
        AvailableModel(
            provider_id=provider_id,
            model_id=model_id,
            display_name=display_name,
            available=available,
            is_llm=True,
            context_window=context_window,
            discovery_source=discovery_source,
            last_checked=checked_at,
        )
        for model_id, display_name, context_window in specs
    ]


def _extract_version(stdout: str, stderr: str) -> str | None:
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not combined:
        return None
    return combined.splitlines()[0][:200]


async def _discover_agy(timeout: float, now: datetime) -> DiscoveryResult:
    executable = shutil.which("agy")
    if not executable:
        return DiscoveryResult(
            provider_id="agy",
            status="missing_binary",
            detail="agy binary not found in PATH",
            models=[],
            discovery_status="unverified",
            checked_at=now,
        )
    version_ok, _, v_out, v_err = await _run_cli_probe(executable, "--version", timeout=min(timeout, 5.0))
    version = _extract_version(v_out, v_err) if version_ok else None

    models_ok, _, m_out, m_err = await _run_cli_probe(executable, "models", timeout=timeout)
    combined = "\n".join(part for part in (m_out, m_err) if part).strip()
    parsed = parse_agy_models_output(m_out, checked_at=now) if models_ok else []
    if models_ok and parsed:
        return DiscoveryResult(
            provider_id="agy",
            status="authenticated",
            detail=f"Found at {executable} ({len(parsed)} models)",
            models=parsed,
            discovery_status="ok",
            version=version,
            checked_at=now,
        )

    lowered = combined.lower()
    if any(marker in lowered for marker in _AUTH_DENIED_MARKERS):
        fallback_unavailable = [
            AvailableModel(
                provider_id="agy",
                model_id=slug,
                display_name=label,
                available=False,
                is_llm=True,
                context_window=1_000_000,
                discovery_source="fallback",
                last_checked=now,
            )
            for slug, label in AgyCliRunner.SUPPORTED_MODEL_SPECS
        ]
        return DiscoveryResult(
            provider_id="agy",
            status="unauthenticated",
            detail="agy CLI found, but authentication is required",
            models=fallback_unavailable,
            discovery_status="error",
            discovery_error="agy authentication required",
            version=version,
            checked_at=now,
        )

    if version_ok:
        fallback_models = [
            AvailableModel(
                provider_id="agy",
                model_id=slug,
                display_name=label,
                available=True,
                is_llm=True,
                context_window=1_000_000,
                discovery_source="fallback",
                last_checked=now,
            )
            for slug, label in AgyCliRunner.SUPPORTED_MODEL_SPECS
        ]
        err_summary = (m_err or m_out or "agy models probe failed").splitlines()[-1][:200]
        return DiscoveryResult(
            provider_id="agy",
            status="authenticated",
            detail=f"Found at {executable} (using fallback model catalog)",
            models=fallback_models,
            discovery_status="fallback",
            discovery_error=err_summary,
            version=version,
            checked_at=now,
        )

    return DiscoveryResult(
        provider_id="agy",
        status="error",
        detail=f"agy binary at {executable} failed health probe",
        models=[],
        discovery_status="error",
        discovery_error=(v_err or m_err or "agy probe failed")[:200],
        checked_at=now,
    )


async def _discover_gemini(timeout: float, now: datetime) -> DiscoveryResult:
    executable = shutil.which("gemini")
    if not executable:
        return DiscoveryResult(
            provider_id="gemini",
            status="missing_binary",
            detail="gemini binary not found in PATH",
            models=[],
            discovery_status="unverified",
            checked_at=now,
        )
    version_ok, _, v_out, v_err = await _run_cli_probe(executable, "--version", timeout=min(timeout, 5.0))
    version = _extract_version(v_out, v_err) if version_ok else None
    is_authed = version_ok and has_gemini_subscription_auth()

    json_ok, _, j_out, j_err = await _run_cli_probe(
        executable, "models", "-o", "json", timeout=timeout
    )
    parsed = parse_gemini_models_json(j_out, checked_at=now) if json_ok else []
    discovery_error: str | None = None
    discovery_status: DiscoveryStatusValue = "ok"

    if not parsed:
        list_ok, _, l_out, l_err = await _run_cli_probe(executable, "--list-models", timeout=timeout)
        parsed = parse_gemini_models_text(l_out, checked_at=now) if list_ok else []
        if not parsed:
            discovery_status = "fallback"
            discovery_error = (j_err or l_err or "gemini models probe returned no models").splitlines()[-1][:200]
            parsed = _build_static_models(
                "gemini",
                _GEMINI_FALLBACK_MODELS,
                available=is_authed,
                discovery_source="fallback",
                checked_at=now,
            )

    models = [item.model_copy(update={"available": is_authed}) for item in parsed]
    if is_authed:
        status: DiscoveryProviderStatus = "authenticated"
        detail = f"Found at {executable} ({len(models)} models)"
    else:
        status = "unauthenticated"
        detail = "gemini CLI found, but no subscription OAuth/GCA/Vertex credentials configured"
    return DiscoveryResult(
        provider_id="gemini",
        status=status,
        detail=detail,
        models=models,
        discovery_status=discovery_status,
        discovery_error=discovery_error,
        version=version,
        checked_at=now,
    )


async def _discover_copilot(timeout: float, now: datetime) -> DiscoveryResult:
    executable = shutil.which("copilot")
    if not executable:
        return DiscoveryResult(
            provider_id="copilot",
            status="missing_binary",
            detail="copilot binary not found in PATH",
            models=[],
            discovery_status="unverified",
            checked_at=now,
        )
    version_ok, _, v_out, v_err = await _run_cli_probe(executable, "--version", timeout=min(timeout, 5.0))
    version = _extract_version(v_out, v_err) if version_ok else None

    is_authed = False
    if version_ok:
        if has_copilot_local_auth():
            is_authed = True
        else:
            gh_exe = shutil.which("gh")
            if gh_exe:
                gh_ok, _, gh_out, gh_err = await _run_cli_probe(gh_exe, "auth", "status", timeout=min(timeout, 5.0))
                gh_combined = "\n".join(p for p in (gh_out, gh_err) if p)
                is_authed = is_auth_output_healthy(gh_ok, gh_combined)

    help_ok, _, h_out, h_err = await _run_cli_probe(executable, "help", "config", timeout=timeout)
    parsed = parse_copilot_help_config_models(h_out, checked_at=now) if help_ok else []
    discovery_status: DiscoveryStatusValue = "ok"
    discovery_error: str | None = None
    if not parsed:
        discovery_status = "fallback"
        discovery_error = (h_err or "copilot help config returned no models").splitlines()[-1][:200]
        parsed = _build_static_models(
            "copilot",
            _COPILOT_FALLBACK_MODELS,
            available=is_authed,
            discovery_source="fallback",
            checked_at=now,
        )

    models = [item.model_copy(update={"available": is_authed}) for item in parsed]
    return DiscoveryResult(
        provider_id="copilot",
        status="authenticated" if is_authed else "unauthenticated",
        detail=f"Found at {executable}" if is_authed else "copilot CLI found, but not authenticated (run copilot login)",
        models=models,
        discovery_status=discovery_status,
        discovery_error=discovery_error,
        version=version,
        checked_at=now,
    )


async def _discover_curated_cli(
    provider_id: str,
    binary: str,
    auth_args: tuple[str, ...],
    curated_specs: tuple[tuple[str, str, int], ...],
    timeout: float,
    now: datetime,
) -> DiscoveryResult:
    executable = shutil.which(binary)
    if not executable:
        models = _build_static_models(
            provider_id,
            curated_specs,
            available=False,
            discovery_source="curated",
            checked_at=now,
        )
        return DiscoveryResult(
            provider_id=provider_id,
            status="missing_binary",
            detail=f"{binary} binary not found in PATH",
            models=models,
            discovery_status="unverified",
            checked_at=now,
        )
    version_ok, _, v_out, v_err = await _run_cli_probe(executable, "--version", timeout=timeout)
    version = _extract_version(v_out, v_err) if version_ok else None
    auth_ok, _, a_out, a_err = await _run_cli_probe(executable, *auth_args, timeout=timeout)
    combined = "\n".join(part for part in (a_out, a_err) if part).strip()
    is_authed = version_ok and is_auth_output_healthy(auth_ok, combined)
    models = _build_static_models(
        provider_id,
        curated_specs,
        available=is_authed,
        discovery_source="curated",
        checked_at=now,
    )
    return DiscoveryResult(
        provider_id=provider_id,
        status="authenticated" if is_authed else "unauthenticated",
        detail=f"Found at {executable}" if is_authed else f"{binary} found at {executable}, but not authenticated",
        models=models,
        discovery_status="ok" if is_authed else "unverified",
        version=version,
        checked_at=now,
    )


async def _discover_groq(
    *, api_key: str | None, timeout: float, now: datetime
) -> DiscoveryResult:
    key = api_key if api_key is not None else get_vault_credential("GROQ_API_KEY")
    if not key:
        return DiscoveryResult(
            provider_id="groq",
            status="unauthenticated",
            detail="No Groq API key configured",
            models=[],
            discovery_status="unverified",
            base_url=DEFAULT_GROQ_BASE_URL,
            checked_at=now,
        )
    url = f"{DEFAULT_GROQ_BASE_URL}/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {key}"})
    except httpx.HTTPError as exc:
        fallback = _build_static_models(
            "groq",
            _GROQ_FALLBACK_MODELS,
            available=True,
            discovery_source="fallback",
            checked_at=now,
        )
        return DiscoveryResult(
            provider_id="groq",
            status="configured",
            detail="Groq API key saved; live model discovery temporarily unreachable",
            models=fallback,
            discovery_status="fallback",
            discovery_error=f"Groq model probe failed ({type(exc).__name__})",
            base_url=DEFAULT_GROQ_BASE_URL,
            checked_at=now,
        )

    if response.status_code in (401, 403):
        return DiscoveryResult(
            provider_id="groq",
            status="expired",
            detail=f"Groq API key rejected (HTTP {response.status_code})",
            models=[],
            discovery_status="error",
            discovery_error=f"Groq API key rejected (HTTP {response.status_code})",
            base_url=DEFAULT_GROQ_BASE_URL,
            checked_at=now,
        )

    if response.is_error:
        fallback = _build_static_models(
            "groq",
            _GROQ_FALLBACK_MODELS,
            available=True,
            discovery_source="fallback",
            checked_at=now,
        )
        return DiscoveryResult(
            provider_id="groq",
            status="configured",
            detail=f"Groq API key saved; /models returned HTTP {response.status_code}",
            models=fallback,
            discovery_status="fallback",
            discovery_error=f"Groq /models returned HTTP {response.status_code}",
            base_url=DEFAULT_GROQ_BASE_URL,
            checked_at=now,
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    items = payload.get("data", []) if isinstance(payload, dict) else []
    models: list[AvailableModel] = []
    seen: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            model_id = raw_id.strip()
            if model_id in seen:
                continue
            seen.add(model_id)
            ctx_val = item.get("context_window")
            context_window = ctx_val if isinstance(ctx_val, int) and ctx_val > 0 else 0
            models.append(
                AvailableModel(
                    provider_id="groq",
                    model_id=model_id,
                    display_name=model_id,
                    available=True,
                    is_llm=_is_groq_llm_model(model_id),
                    context_window=context_window,
                    discovery_source="live",
                    last_checked=now,
                )
            )
    llm_count = sum(1 for m in models if m.is_llm)
    return DiscoveryResult(
        provider_id="groq",
        status="configured",
        detail=f"Groq API verified ({llm_count} LLM models discovered)",
        models=models,
        discovery_status="ok",
        base_url=DEFAULT_GROQ_BASE_URL,
        checked_at=now,
    )


async def _discover_custom_api(
    *, base_url: str | None, api_key: str | None, timeout: float, now: datetime
) -> DiscoveryResult:
    key = api_key if api_key is not None else get_vault_credential("DAGGER_API_KEY")
    raw_url = base_url if base_url is not None else get_base_url("api")
    if not key:
        return DiscoveryResult(
            provider_id="api",
            status="unauthenticated",
            detail="No custom API key configured",
            models=[],
            discovery_status="unverified",
            base_url=raw_url,
            checked_at=now,
        )
    if not raw_url:
        return DiscoveryResult(
            provider_id="api",
            status="configured",
            detail="API key configured; set Base URL to discover available models",
            models=[],
            discovery_status="unverified",
            base_url=None,
            checked_at=now,
        )
    try:
        clean_url = validate_base_url(raw_url)
    except ValueError:
        return DiscoveryResult(
            provider_id="api",
            status="error",
            detail="Invalid custom API base URL",
            models=[],
            discovery_status="error",
            discovery_error="Invalid API base URL (must be HTTP(S) without embedded credentials)",
            base_url=None,
            checked_at=now,
        )

    parsed_url = urlsplit(clean_url)
    is_anthropic = (parsed_url.hostname or "").lower() == "api.anthropic.com"
    stripped = clean_url.rstrip("/")
    if is_anthropic:
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01"}
        candidate_urls = (
            [stripped]
            if stripped.endswith("/models")
            else ([f"{stripped}/models"] if stripped.endswith("/v1") else [f"{stripped}/v1/models"])
        )
    else:
        headers = {"Authorization": f"Bearer {key}"}
        if stripped.endswith("/models"):
            candidate_urls = [stripped]
        elif re.search(r"/v\d+$", stripped):
            candidate_urls = [f"{stripped}/models"]
        else:
            candidate_urls = [f"{stripped}/models", f"{stripped}/v1/models"]

    response: httpx.Response | None = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for candidate in candidate_urls:
                response = await client.get(candidate, headers=headers)
                if response.status_code != 404:
                    break
    except httpx.HTTPError as exc:
        return DiscoveryResult(
            provider_id="api",
            status="unreachable",
            detail=f"Custom API endpoint unreachable ({type(exc).__name__})",
            models=[],
            discovery_status="error",
            discovery_error=f"Custom API endpoint unreachable ({type(exc).__name__})",
            base_url=clean_url,
            checked_at=now,
        )

    assert response is not None
    if response.status_code in (401, 403):
        return DiscoveryResult(
            provider_id="api",
            status="expired",
            detail=f"API key rejected (HTTP {response.status_code})",
            models=[],
            discovery_status="error",
            discovery_error=f"API key rejected (HTTP {response.status_code})",
            base_url=clean_url,
            checked_at=now,
        )
    if response.is_error:
        return DiscoveryResult(
            provider_id="api",
            status="error",
            detail=f"Model discovery failed (HTTP {response.status_code})",
            models=[],
            discovery_status="error",
            discovery_error=f"Model endpoint returned HTTP {response.status_code}",
            base_url=clean_url,
            checked_at=now,
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {}
    items = payload.get("data", []) if isinstance(payload, dict) else []
    models: list[AvailableModel] = []
    seen: set[str] = set()
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_id = item.get("id")
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            model_id = raw_id.strip()
            if model_id in seen:
                continue
            seen.add(model_id)
            display_name = item.get("display_name") or item.get("name") or model_id
            ctx_val = item.get("context_window") or item.get("context_length")
            context_window = ctx_val if isinstance(ctx_val, int) and ctx_val > 0 else 0
            models.append(
                AvailableModel(
                    provider_id="api",
                    model_id=model_id,
                    display_name=str(display_name).strip() or model_id,
                    available=True,
                    is_llm=True,
                    context_window=context_window,
                    discovery_source="live",
                    last_checked=now,
                )
            )
    return DiscoveryResult(
        provider_id="api",
        status="configured",
        detail=f"Custom API verified ({len(models)} models discovered)",
        models=models,
        discovery_status="ok",
        base_url=clean_url,
        checked_at=now,
    )


async def _discover_local(
    *, base_url: str | None, api_key: str | None, timeout: float, now: datetime
) -> DiscoveryResult:
    raw_url = base_url if base_url is not None else (get_base_url("local") or DEFAULT_LOCAL_BASE_URL)
    try:
        clean_url = validate_base_url(raw_url)
    except ValueError:
        return DiscoveryResult(
            provider_id="local",
            status="error",
            detail="Invalid local endpoint base URL",
            models=[],
            discovery_status="error",
            discovery_error="Invalid local base URL (must be HTTP(S) without embedded credentials)",
            base_url=None,
            checked_at=now,
        )

    root = clean_url.rstrip("/")
    if root.endswith("/v1"):
        root = root[:-3]
    effective_base_url = f"{root}/v1"

    key = api_key if api_key is not None else get_vault_credential("DAGGER_LOCAL_API_KEY")
    headers = {"Authorization": f"Bearer {key}"} if key else {}

    models: list[AvailableModel] = []
    seen: set[str] = set()
    reachable = False
    last_error: str | None = None

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(f"{root}/v1/models", headers=headers)
            if not resp.is_error:
                reachable = True
                try:
                    data = resp.json().get("data", [])
                except (ValueError, AttributeError):
                    data = []
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
                            model_id = item["id"].strip()
                            if model_id not in seen:
                                seen.add(model_id)
                                models.append(
                                    AvailableModel(
                                        provider_id="local",
                                        model_id=model_id,
                                        display_name=model_id,
                                        available=True,
                                        is_llm=True,
                                        context_window=0,
                                        discovery_source="live",
                                        last_checked=now,
                                    )
                                )
            else:
                last_error = f"HTTP {resp.status_code}"

            if not models:
                # Ollama fallback: GET /api/tags
                tags_resp = await client.get(f"{root}/api/tags", headers=headers)
                if not tags_resp.is_error:
                    reachable = True
                    try:
                        tags_data = tags_resp.json().get("models", [])
                    except (ValueError, AttributeError):
                        tags_data = []
                    if isinstance(tags_data, list):
                        for item in tags_data:
                            if not isinstance(item, dict):
                                continue
                            raw_name = item.get("name") or item.get("model")
                            if isinstance(raw_name, str) and raw_name.strip():
                                model_id = raw_name.strip()
                                if model_id not in seen:
                                    seen.add(model_id)
                                    models.append(
                                        AvailableModel(
                                            provider_id="local",
                                            model_id=model_id,
                                            display_name=model_id,
                                            available=True,
                                            is_llm=True,
                                            context_window=0,
                                            discovery_source="live",
                                            last_checked=now,
                                        )
                                    )
                elif not reachable:
                    last_error = f"HTTP {tags_resp.status_code}"
    except httpx.HTTPError as exc:
        return DiscoveryResult(
            provider_id="local",
            status="unreachable",
            detail=f"Local endpoint unreachable at {effective_base_url}",
            models=[],
            discovery_status="error",
            discovery_error=f"Local endpoint unreachable ({type(exc).__name__})",
            base_url=effective_base_url,
            checked_at=now,
        )

    if models:
        return DiscoveryResult(
            provider_id="local",
            status="authenticated",
            detail=f"Healthy local endpoint at {effective_base_url} ({len(models)} models)",
            models=models,
            discovery_status="ok",
            base_url=effective_base_url,
            checked_at=now,
        )
    if reachable:
        return DiscoveryResult(
            provider_id="local",
            status="configured",
            detail=f"Local endpoint reachable at {effective_base_url}, but no models loaded",
            models=[],
            discovery_status="ok",
            base_url=effective_base_url,
            checked_at=now,
        )
    return DiscoveryResult(
        provider_id="local",
        status="unreachable",
        detail=f"Local endpoint at {effective_base_url} returned {last_error or 'error'}",
        models=[],
        discovery_status="error",
        discovery_error=f"Local endpoint returned {last_error or 'error'}",
        base_url=effective_base_url,
        checked_at=now,
    )


async def _discover_tavily(
    *, api_key: str | None, now: datetime
) -> DiscoveryResult:
    key = api_key if api_key is not None else get_vault_credential("TAVILY_API_KEY")
    cleaned = (key or "").strip()
    configured = bool(cleaned) and "\r" not in cleaned and "\n" not in cleaned
    model = AvailableModel(
        provider_id="tavily",
        model_id="tavily-search",
        display_name="Tavily Search",
        available=configured,
        is_llm=False,
        context_window=0,
        discovery_source="static",
        last_checked=now,
    )
    return DiscoveryResult(
        provider_id="tavily",
        status="configured" if configured else "unauthenticated",
        detail="Tavily search key configured (tool provider)" if configured else "No Tavily API key configured",
        models=[model],
        discovery_status="ok" if configured else "unverified",
        checked_at=now,
    )


async def discover_provider_models(
    provider_id: str,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float | None = None,
) -> DiscoveryResult:
    """Discover auth readiness and available models for any listed service provider."""
    pid = provider_id.strip().lower()
    now = _utc_now()
    if pid == "agy":
        return await _discover_agy(timeout=timeout if timeout is not None else 8.0, now=now)
    if pid == "gemini":
        return await _discover_gemini(timeout=timeout if timeout is not None else 8.0, now=now)
    if pid == "copilot":
        return await _discover_copilot(timeout=timeout if timeout is not None else 5.0, now=now)
    if pid == "claude":
        return await _discover_curated_cli(
            "claude",
            "claude",
            ("auth", "status"),
            _CLAUDE_CURATED_MODELS,
            timeout=timeout if timeout is not None else 5.0,
            now=now,
        )
    if pid == "codex":
        return await _discover_curated_cli(
            "codex",
            "codex",
            ("login", "status"),
            _CODEX_CURATED_MODELS,
            timeout=timeout if timeout is not None else 5.0,
            now=now,
        )
    if pid == "groq":
        return await _discover_groq(
            api_key=api_key, timeout=timeout if timeout is not None else 5.0, now=now
        )
    if pid == "api":
        return await _discover_custom_api(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout if timeout is not None else 5.0,
            now=now,
        )
    if pid == "local":
        return await _discover_local(
            base_url=base_url,
            api_key=api_key,
            timeout=timeout if timeout is not None else 3.0,
            now=now,
        )
    if pid == "tavily":
        return await _discover_tavily(api_key=api_key, now=now)
    raise ValueError(f"Unknown service provider: {provider_id}")
