"""Discover available endpoints, synthesize agent containers, and schedule refreshes."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import html
import json
import logging
import os
from pathlib import Path
import re
import shutil
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yaml
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from dagger.providers.cli_runners import AgyCliRunner
from dagger.providers.discovery import (
    CLI_PROBE_STRIPPED_ENV,
    DEFAULT_GROQ_BASE_URL,
    SERVICE_PROVIDER_BY_ID,
    SERVICE_PROVIDER_METADATA,
    DiscoveryResult,
    _CLAUDE_CURATED_MODELS,
    _CODEX_CURATED_MODELS,
    _COPILOT_FALLBACK_MODELS,
    _GEMINI_FALLBACK_MODELS,
    discover_provider_models,
    has_copilot_local_auth,
    has_gemini_subscription_auth,
    is_auth_output_healthy,
)
from dagger.schema import AgentContainer, AvailableModel, EndpointCapability
from dagger.vault import get_base_url, get_vault_credential, load_vault_credentials

if TYPE_CHECKING:
    from dagger.db import DatabaseManager

logger = logging.getLogger(__name__)
DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "containers" / "seed_containers.yaml"
_GENERATORS: dict[str, "AgentGenerator"] = {}
_AUTH_COMMANDS = {
    "claude": ("auth", "status"),
    "codex": ("login", "status"),
}
_API_KEYS = CLI_PROBE_STRIPPED_ENV
_DOWNSHIFT_DEFAULTS: dict[str, str] = {
    "claude": "claude-haiku-4-5",
    "codex": "gpt-4.1-mini",
    "gemini": "gemini-3.5-flash",
    "copilot": "gpt-5-mini",
    "groq": "llama-3.1-8b-instant",
    "api": "gpt-4o-mini",
    "agy": "gemini-4-hydrogen-low",
}
_LIGHTWEIGHT_MODEL_MARKERS: tuple[str, ...] = (
    "mini",
    "flash",
    "lite",
    "haiku",
    "instant",
    "8b",
    "7b",
    "3b",
    "hydrogen",
    "nano",
    "small",
)
_COST_RANK_BY_TIER: dict[str, int] = {
    "local_free": 0,
    "flat_subscription": 1,
    "metered_api": 2,
}
_CLI_BASELINES = {
    "claude": ("claude-sonnet-4-5", 200_000, ["architecture", "reasoning"], ["Requires Claude CLI authentication"]),
    "codex": ("gpt-5-codex", 128_000, ["coding", "testing"], ["Requires Codex CLI authentication"]),
    "gemini": ("gemini-2.5-pro", 1_000_000, ["analysis", "review"], ["Requires Gemini CLI authentication"]),
    "copilot": ("gpt-4.1", 128_000, ["coding", "patching"], ["Requires Copilot CLI authentication"]),
    "agy": ("Gemini 4 Hydrogen (Low)", 1_000_000, ["review", "analysis"], ["Requires Antigravity CLI"]),
}
_RESEARCH_QUERIES = (
    "recent AI model releases and capabilities",
    "AI model strengths and context window sizes",
    "claude codex gemini copilot CLI flags and rate limits",
)
_SUSPICIOUS_RESEARCH = re.compile(
    r"(?i)(?:ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions|"
    r"reveal\s+(?:the\s+)?(?:system\s+prompt|credentials|secrets)|"
    r"<\s*script\b|javascript\s*:)"
)


def infer_service_provider(container: AgentContainer) -> str:
    """Classify a container into its canonical 9-provider service ID."""
    provider = container.provider.strip().lower()
    if provider != "api":
        return provider
    cid = container.container_id.strip().lower()
    burl = (container.base_url or "").strip().lower()
    model = (container.model or "").strip().lower()
    if "groq" in cid or "groq.com" in burl:
        return "groq"
    if "tavily" in cid or model == "tavily-search":
        return "tavily"
    if container.cost_tier == "local_free" or cid == "local_triage":
        return "local"
    if burl:
        host = (urlsplit(burl).hostname or "").lower()
        if host in {"127.0.0.1", "localhost", "::1"} and "custom" not in cid:
            return "local"
    return "api"


def match_available_model(
    provider_id: str,
    model: str | None,
    available_models: list[AvailableModel],
) -> AvailableModel | None:
    """Match a model string against a provider's available models without cross-provider fallback."""
    if not model or not model.strip() or not available_models:
        return None
    raw = model.strip()
    lowered = raw.lower()
    for item in available_models:
        if item.model_id.lower() == lowered:
            return item
        if item.display_name and item.display_name.lower() == lowered:
            return item

    if provider_id.strip().lower() == "agy":
        # Resolve exact agy label / slug / punctuation aliases WITHOUT falling back to unknown defaults.
        mapped = AgyCliRunner._LABEL_TO_SLUG.get(lowered)
        if mapped is None:
            slug_candidate = lowered.replace("_", "-").replace(" ", "-")
            mapped = AgyCliRunner._SLUG_MAP.get(slug_candidate)
            if mapped is None and slug_candidate in AgyCliRunner._FAMILY_ALIASES:
                family = AgyCliRunner._FAMILY_ALIASES[slug_candidate]
                default_tier = AgyCliRunner._DEFAULT_TIER_BY_FAMILY[family]
                mapped = AgyCliRunner._FAMILY_TIERS[family][default_tier]
        if mapped:
            for item in available_models:
                if item.model_id.lower() == mapped.lower():
                    return item
    return None


def _pick_lighter_available_model(
    service_provider: str,
    current_model: str | None,
    available_llms: list[AvailableModel],
) -> AvailableModel | None:
    """Select the best lighter/cheaper available LLM for a single provider."""
    if not available_llms:
        return None
    current_match = match_available_model(service_provider, current_model, available_llms)
    current_id = current_match.model_id.lower() if current_match else (current_model or "").strip().lower()

    preferred = _DOWNSHIFT_DEFAULTS.get(service_provider)
    if preferred:
        pref_match = match_available_model(service_provider, preferred, available_llms)
        if pref_match is not None and pref_match.model_id.lower() != current_id:
            return pref_match

    def _is_light(m: AvailableModel) -> bool:
        hay = f"{m.model_id} {m.display_name or ''}".lower()
        return any(marker in hay for marker in _LIGHTWEIGHT_MODEL_MARKERS)

    others = [m for m in available_llms if m.model_id.lower() != current_id]
    light_others = [m for m in others if _is_light(m)]
    if light_others:
        return light_others[0]
    if preferred:
        pref_match = match_available_model(service_provider, preferred, available_llms)
        if pref_match is not None:
            return pref_match
    if others:
        return others[0]
    return available_llms[0]


async def _run_scheduled_generator(key: str) -> None:
    generator = _GENERATORS.get(key)
    if generator is None:
        logger.warning("No generator registered for scheduled job %s", key)
        return
    await generator.refresh_catalog()


def _has_gemini_auth() -> bool:
    """Return True when Gemini CLI has valid subscription OAuth/GCA/Vertex credentials."""
    return has_gemini_subscription_auth()


class AgentGenerator:
    """Refresh catalog through async database upsert/list methods and export YAML."""

    def __init__(
        self,
        db: DatabaseManager,
        *,
        seed_path: str | Path | None = None,
        export_dir: str | Path = "containers",
        job_store_path: str | Path = "data/scheduler.sqlite3",
        interval_hours: float = 24,
        tavily_api_key: str | None = None,
        local_base_url: str = "http://127.0.0.1:8080",
    ) -> None:
        if interval_hours <= 0:
            raise ValueError("interval_hours must be positive")
        self.db = db
        self.seed_path = Path(seed_path) if seed_path is not None else DEFAULT_SEED_PATH
        self.export_dir = Path(export_dir)
        self.job_store_path = Path(job_store_path).resolve()
        self.interval_hours = interval_hours
        self.tavily_api_key = tavily_api_key if tavily_api_key is not None else os.getenv("TAVILY_API_KEY")
        self.local_base_url = local_base_url.rstrip("/")
        if self.local_base_url.endswith("/v1"):
            self.local_base_url = self.local_base_url[:-3]
        self.scheduler: AsyncIOScheduler | None = None
        self._job_key = str(self.job_store_path)
        self._run_lock = asyncio.Lock()
        self._scheduled_tasks: set[asyncio.Task] = set()
        self.research_findings: list[dict[str, str]] = []
        self.last_discovery: dict[str, DiscoveryResult] = {}

    def start(self) -> None:
        """Start the persistent scheduler from a running event loop."""
        asyncio.get_running_loop()
        if self.scheduler and self.scheduler.running:
            return
        if self._job_key in _GENERATORS:
            raise RuntimeError(f"A generator already owns {self.job_store_path}")
        self.job_store_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(URL.create("sqlite", database=str(self.job_store_path)))

        @event.listens_for(engine, "connect")
        def configure_sqlite(connection, _record):
            cursor = connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        scheduler = AsyncIOScheduler(jobstores={"default": SQLAlchemyJobStore(engine=engine)})
        _GENERATORS[self._job_key] = self
        try:
            scheduler.start(paused=True)
            if scheduler.get_job("generator-refresh") is None:
                scheduler.add_job(
                    _run_scheduled_generator, "interval", hours=self.interval_hours,
                    args=[self._job_key], id="generator-refresh", max_instances=1, coalesce=True,
                )
            scheduler.resume()
        except Exception:
            if scheduler.running:
                scheduler.shutdown(wait=False)
            _GENERATORS.pop(self._job_key, None)
            raise
        self.scheduler = scheduler

    def shutdown(self) -> None:
        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        _GENERATORS.pop(self._job_key, None)
        self.scheduler = None

    async def aclose(self) -> None:
        """Cancel scheduled work before the caller closes its database."""
        self.shutdown()
        await asyncio.sleep(0)
        if self._scheduled_tasks:
            await asyncio.gather(*self._scheduled_tasks, return_exceptions=True)

    async def _command(self, *args: str) -> tuple[bool, str]:
        env = os.environ.copy()
        for key in _API_KEYS:
            env.pop(key, None)
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env,
            )
        except OSError:
            return False, ""
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return False, ""
        return process.returncode == 0, (stdout + b"\n" + stderr).decode(errors="replace").strip()

    @staticmethod
    def _is_auth_output_healthy(auth_ok: bool, status: str) -> bool:
        return is_auth_output_healthy(auth_ok, status)

    async def _probe_cli(self, container: AgentContainer) -> EndpointCapability:
        executable = shutil.which(container.provider)
        version = None
        healthy = False
        if executable:
            version_ok, output = await self._command(executable, "--version")
            if version_ok and output:
                version = output.splitlines()[0][:200]
            if container.provider == "agy":
                healthy = version_ok
            elif container.provider == "gemini":
                healthy = version_ok and _has_gemini_auth()
            elif container.provider == "copilot":
                if version_ok:
                    if has_copilot_local_auth():
                        healthy = True
                    else:
                        gh_exe = shutil.which("gh")
                        if gh_exe:
                            gh_ok, gh_status = await self._command(gh_exe, "auth", "status")
                            healthy = self._is_auth_output_healthy(gh_ok, gh_status)
            elif container.provider in _AUTH_COMMANDS:
                auth_ok, status = await self._command(executable, *_AUTH_COMMANDS[container.provider])
                healthy = version_ok and self._is_auth_output_healthy(auth_ok, status)
        baseline_model, context, strengths, weaknesses = _CLI_BASELINES[container.provider]
        return EndpointCapability(
            provider=container.provider, model_name=container.model or "default", cost_tier="flat_subscription",
            max_context_tokens=context if container.model == baseline_model else 0,
            strengths=list(dict.fromkeys(container.strengths + strengths)),
            weaknesses=weaknesses + ["Context window is an estimate; verify current provider limits"],
            recommended_roles=[container.role],
            healthy=healthy, available=healthy, service_provider=container.provider,
            version=version, last_checked=datetime.now(timezone.utc),
        )

    def _effective_local_root(self) -> str:
        configured = get_base_url("local")
        raw = configured if configured else self.local_base_url
        root = raw.rstrip("/")
        if root.endswith("/v1"):
            root = root[:-3]
        return root

    async def _probe_local(self) -> list[str]:
        root = self._effective_local_root()
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{root}/v1/models")
                response.raise_for_status()
                data = response.json().get("data", [])
                if not isinstance(data, list):
                    return []
                return list(dict.fromkeys(
                    item["id"] for item in data
                    if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
                ))
        except (httpx.HTTPError, ValueError, AttributeError):
            return []

    async def discover_web(self) -> list[dict[str, str]]:
        """Return optional, untrusted research snippets; never execute discovered commands."""
        if not self.tavily_api_key:
            return []
        try:
            from tavily import AsyncTavilyClient
            client = AsyncTavilyClient(api_key=self.tavily_api_key)
            responses = await asyncio.wait_for(asyncio.gather(*(
                client.search(query=query, max_results=3) for query in _RESEARCH_QUERIES
            )), timeout=15)
            findings = []
            for query, response in zip(_RESEARCH_QUERIES, responses):
                for item in response.get("results", []):
                    if not isinstance(item, dict):
                        continue
                    url = item.get("url")
                    if not isinstance(url, str) or len(url) > 2048 or any(ord(char) < 33 for char in url):
                        continue
                    try:
                        parsed = urlsplit(url)
                        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                            continue
                        parsed.port  # Validate the optional port.
                    except ValueError:
                        continue
                    title = re.sub(r"[\x00-\x1f\x7f]", " ", html.unescape(str(item.get("title") or "")))[:100]
                    content = re.sub(
                        r"[\x00-\x09\x0b-\x1f\x7f]", " ", html.unescape(str(item.get("content") or ""))
                    )[:500]
                    if _SUSPICIOUS_RESEARCH.search(f"{title} {content}"):
                        continue
                    findings.append({"query": query, "title": title, "url": url, "content": content})
            return findings
        except Exception:
            logger.warning("Tavily discovery unavailable; continuing with local capabilities")
            return []

    def _apply_research(self, capability: EndpointCapability) -> None:
        """Attach explicitly labeled, provider-specific findings as unverified data."""
        for finding in self.research_findings:
            text = f"{finding['title']}\n{finding['content']}"
            lower = text.lower()
            mentioned = {provider for provider in _CLI_BASELINES if re.search(
                rf"\b{re.escape(provider)}\b", lower
            )}
            if capability.provider not in mentioned or len(mentioned) != 1:
                continue
            source = finding["url"]
            for label, destination in (("strengths?", capability.strengths), ("weaknesses?", capability.weaknesses)):
                match = re.search(rf"\b{label}\s*:\s*([^\n.]{{1,200}})", text, re.IGNORECASE)
                if match:
                    value = f"Unverified: {match.group(1).strip()} ({source})"
                    if value not in destination:
                        destination.append(value)
            match = re.search(
                r"\bcontext(?:\s+window)?\s*(?:of|:|is|=)?\s*([\d,]{1,16}(?:\.\d{1,3})?)\s*([km]?)\s*(?:tokens?)?\b",
                text, re.IGNORECASE,
            )
            if match:
                raw_num = match.group(1).replace(",", "").strip(".")
                if raw_num:
                    number = float(raw_num)
                    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
                    tokens = int(number * multiplier)
                    if 0 < tokens <= 100_000_000:
                        capability.max_context_tokens = max(capability.max_context_tokens, tokens)

    async def _discover_cli_via_command_override(self, pid: str) -> DiscoveryResult:
        """Honor unit-test overrides on `self._command` while producing a DiscoveryResult."""
        now = datetime.now(timezone.utc)
        executable = shutil.which(pid)
        if not executable:
            specs_map = {
                "claude": _CLAUDE_CURATED_MODELS,
                "codex": _CODEX_CURATED_MODELS,
            }
            models = [
                AvailableModel(
                    provider_id=pid,
                    model_id=mid,
                    display_name=lbl,
                    available=False,
                    is_llm=True,
                    context_window=ctx,
                    discovery_source="curated",
                    last_checked=now,
                )
                for mid, lbl, ctx in specs_map.get(pid, ())
            ]
            return DiscoveryResult(
                provider_id=pid,
                status="missing_binary",
                detail=f"{pid} binary not found in PATH",
                models=models,
                discovery_status="unverified",
                checked_at=now,
            )
        version_ok, output = await self._command(executable, "--version")
        version = output.splitlines()[0][:200] if version_ok and output else None
        healthy = False
        if pid == "agy":
            healthy = version_ok
        elif pid == "gemini":
            healthy = version_ok and _has_gemini_auth()
        elif pid == "copilot":
            if version_ok:
                if has_copilot_local_auth():
                    healthy = True
                else:
                    gh_exe = shutil.which("gh")
                    if gh_exe:
                        gh_ok, gh_status = await self._command(gh_exe, "auth", "status")
                        healthy = self._is_auth_output_healthy(gh_ok, gh_status)
        elif pid in _AUTH_COMMANDS:
            auth_ok, status_out = await self._command(executable, *_AUTH_COMMANDS[pid])
            healthy = version_ok and self._is_auth_output_healthy(auth_ok, status_out)

        if pid == "agy":
            models = [
                AvailableModel(
                    provider_id="agy",
                    model_id=slug,
                    display_name=label,
                    available=healthy,
                    is_llm=True,
                    context_window=1_000_000,
                    discovery_source="fallback",
                    last_checked=now,
                )
                for slug, label in AgyCliRunner.SUPPORTED_MODEL_SPECS
            ]
        else:
            catalog_map = {
                "claude": (_CLAUDE_CURATED_MODELS, "curated"),
                "codex": (_CODEX_CURATED_MODELS, "curated"),
                "gemini": (_GEMINI_FALLBACK_MODELS, "fallback"),
                "copilot": (_COPILOT_FALLBACK_MODELS, "fallback"),
            }
            specs, source = catalog_map[pid]
            models = [
                AvailableModel(
                    provider_id=pid,
                    model_id=mid,
                    display_name=lbl,
                    available=healthy,
                    is_llm=True,
                    context_window=ctx,
                    discovery_source=source,
                    last_checked=now,
                )
                for mid, lbl, ctx in specs
            ]
        return DiscoveryResult(
            provider_id=pid,
            status="authenticated" if healthy else "unauthenticated",
            detail=f"Found at {executable}" if healthy else f"{pid} found at {executable}, but not authenticated",
            models=models,
            discovery_status="ok" if healthy else "unverified",
            version=version,
            checked_at=now,
        )

    async def _discover_for_provider(
        self, provider_id: str, *, vault_creds: dict[str, str] | None = None
    ) -> DiscoveryResult:
        pid = provider_id.strip().lower()
        now = datetime.now(timezone.utc)
        creds = vault_creds if vault_creds is not None else {}

        if pid == "local":
            root = self._effective_local_root()
            v1_url = f"{root}/v1"
            if "_probe_local" in self.__dict__:
                model_ids = await self._probe_local()
                models = [
                    AvailableModel(
                        provider_id="local",
                        model_id=mid,
                        display_name=mid,
                        available=True,
                        is_llm=True,
                        context_window=0,
                        discovery_source="live",
                        last_checked=now,
                    )
                    for mid in model_ids
                ]
                return DiscoveryResult(
                    provider_id="local",
                    status="authenticated" if models else "unreachable",
                    detail=f"Healthy local endpoint at {v1_url}" if models else f"Local endpoint unreachable at {v1_url}",
                    models=models,
                    discovery_status="ok" if models else "error",
                    base_url=v1_url,
                    checked_at=now,
                )
            return await discover_provider_models("local", base_url=v1_url)

        if pid in _CLI_BASELINES:
            if "_command" in self.__dict__:
                return await self._discover_cli_via_command_override(pid)
            return await discover_provider_models(pid)

        if pid == "groq":
            key = creds.get("GROQ_API_KEY") or get_vault_credential("GROQ_API_KEY") or ""
            return await discover_provider_models("groq", api_key=key)

        if pid == "api":
            key = creds.get("DAGGER_API_KEY") or get_vault_credential("DAGGER_API_KEY") or ""
            burl = creds.get("DAGGER_API_BASE_URL") if "DAGGER_API_BASE_URL" in creds else get_base_url("api")
            return await discover_provider_models("api", base_url=burl, api_key=key)

        if pid == "tavily":
            key = (
                creds.get("TAVILY_API_KEY")
                or get_vault_credential("TAVILY_API_KEY")
                or self.tavily_api_key
                or ""
            )
            return await discover_provider_models("tavily", api_key=key)

        raise ValueError(f"Unknown service provider: {provider_id}")

    async def _sync_provider_discovery(
        self,
        result: DiscoveryResult,
        *,
        containers_by_id: dict[str, AgentContainer],
        previous_capabilities: dict[tuple[str, str], EndpointCapability],
        vault_creds: dict[str, str],
        export_updated: bool = True,
    ) -> list[AgentContainer]:
        """Persist provider_models, sync EndpointCapability rows, and reconcile matching containers."""
        pid = result.provider_id
        self.last_discovery[pid] = result
        await self.db.replace_provider_models(pid, result.models)

        avail_llms = [m for m in result.models if m.available and m.is_llm]
        updated_containers: list[AgentContainer] = []

        if pid == "groq":
            has_key = bool(vault_creds.get("GROQ_API_KEY") or os.environ.get("GROQ_API_KEY", "").strip())
            if has_key and "groq_fast_router" not in containers_by_id:
                default_model = avail_llms[0].model_id if avail_llms else "llama-3.3-70b-versatile"
                preferred = match_available_model("groq", "llama-3.3-70b-versatile", avail_llms)
                containers_by_id["groq_fast_router"] = AgentContainer(
                    container_id="groq_fast_router",
                    name="Groq Fast Router",
                    provider="api",
                    base_url=result.base_url or DEFAULT_GROQ_BASE_URL,
                    model=preferred.model_id if preferred else default_model,
                    role="fast_router",
                    cost_tier="metered_api",
                    allowed_tools=["read", "search"],
                    strengths=["triage", "routing", "review", "analysis"],
                    active=True,
                )
            for c in list(containers_by_id.values()):
                if infer_service_provider(c) != "groq":
                    continue
                changed = False
                target_url = result.base_url or DEFAULT_GROQ_BASE_URL
                if c.base_url != target_url:
                    c.base_url = target_url
                    changed = True
                if avail_llms and match_available_model("groq", c.model, avail_llms) is None:
                    preferred = match_available_model("groq", "llama-3.3-70b-versatile", avail_llms)
                    c.model = preferred.model_id if preferred else avail_llms[0].model_id
                    changed = True
                c.active = has_key and result.status in ("configured", "authenticated") and (
                    bool(avail_llms) or result.discovery_status == "unverified"
                )
                if changed:
                    c.version += 1
                updated_containers.append(c)

            if result.models:
                for m in result.models:
                    await self.db.upsert_capability(
                        EndpointCapability(
                            provider="api",
                            model_name=m.model_id,
                            cost_tier="metered_api",
                            max_context_tokens=m.context_window,
                            strengths=["triage", "routing", "review", "analysis"] if m.is_llm else [],
                            recommended_roles=["fast_router"] if m.is_llm else [],
                            healthy=m.available,
                            available=m.available,
                            service_provider="groq",
                            display_name=m.display_name,
                            is_llm=m.is_llm,
                            discovery_source=m.discovery_source,
                            last_checked=result.checked_at,
                        )
                    )
            else:
                for cap in await self.db.list_capabilities():
                    if cap.service_provider == "groq":
                        cap.healthy = False
                        cap.available = False
                        cap.last_checked = result.checked_at
                        await self.db.upsert_capability(cap)

        elif pid == "api":
            has_key = bool(vault_creds.get("DAGGER_API_KEY") or os.environ.get("DAGGER_API_KEY", "").strip())
            if has_key and "custom_api_agent" not in containers_by_id:
                default_model = (
                    avail_llms[0].model_id
                    if avail_llms
                    else os.getenv("DAGGER_API_MODEL", "gpt-4o-mini")
                )
                containers_by_id["custom_api_agent"] = AgentContainer(
                    container_id="custom_api_agent",
                    name="Custom API Agent",
                    provider="api",
                    model=default_model,
                    base_url=result.base_url,
                    role="worker",
                    cost_tier="metered_api",
                    allowed_tools=["read", "search", "write", "edit"],
                    strengths=["coding", "analysis"],
                    active=True,
                )
            for c in list(containers_by_id.values()):
                if infer_service_provider(c) != "api":
                    continue
                changed = False
                if c.container_id == "custom_api_agent" and c.base_url != result.base_url:
                    c.base_url = result.base_url
                    changed = True
                if avail_llms and match_available_model("api", c.model, avail_llms) is None:
                    c.model = avail_llms[0].model_id
                    changed = True
                c.active = has_key and result.status in ("configured", "authenticated")
                if changed:
                    c.version += 1
                updated_containers.append(c)

            if result.models:
                for m in result.models:
                    await self.db.upsert_capability(
                        EndpointCapability(
                            provider="api",
                            model_name=m.model_id,
                            cost_tier="metered_api",
                            max_context_tokens=m.context_window,
                            strengths=["coding", "analysis"],
                            recommended_roles=["worker"],
                            healthy=m.available,
                            available=m.available,
                            service_provider="api",
                            display_name=m.display_name,
                            is_llm=m.is_llm,
                            discovery_source=m.discovery_source,
                            last_checked=result.checked_at,
                        )
                    )
            elif has_key and result.status == "configured" and "custom_api_agent" in containers_by_id:
                c = containers_by_id["custom_api_agent"]
                await self.db.upsert_capability(
                    EndpointCapability(
                        provider="api",
                        model_name=c.model or "gpt-4o-mini",
                        cost_tier="metered_api",
                        strengths=c.strengths,
                        recommended_roles=[c.role],
                        healthy=True,
                        available=True,
                        service_provider="api",
                        is_llm=True,
                        discovery_source="static",
                        last_checked=result.checked_at,
                    )
                )
            else:
                for cap in await self.db.list_capabilities():
                    if cap.service_provider == "api":
                        cap.healthy = False
                        cap.available = False
                        cap.last_checked = result.checked_at
                        await self.db.upsert_capability(cap)

        elif pid == "tavily":
            has_key = bool(
                vault_creds.get("TAVILY_API_KEY")
                or os.environ.get("TAVILY_API_KEY", "").strip()
                or self.tavily_api_key
            )
            if has_key and "tavily_researcher" not in containers_by_id:
                containers_by_id["tavily_researcher"] = AgentContainer(
                    container_id="tavily_researcher",
                    name="Tavily Researcher",
                    provider="api",
                    model="tavily-search",
                    role="researcher",
                    cost_tier="metered_api",
                    allowed_tools=["search"],
                    strengths=["web_search", "research", "discovery"],
                    active=True,
                )
            for c in list(containers_by_id.values()):
                if infer_service_provider(c) != "tavily":
                    continue
                c.active = has_key and result.status == "configured"
                updated_containers.append(c)
            if has_key:
                await self.db.upsert_capability(
                    EndpointCapability(
                        provider="api",
                        model_name="tavily-search",
                        cost_tier="metered_api",
                        strengths=["web_search", "research", "discovery"],
                        recommended_roles=["researcher"],
                        healthy=True,
                        available=True,
                        service_provider="tavily",
                        display_name="Tavily Search",
                        is_llm=False,
                        discovery_source="static",
                        last_checked=result.checked_at,
                    )
                )
            else:
                for cap in await self.db.list_capabilities():
                    if cap.service_provider == "tavily":
                        cap.healthy = False
                        cap.available = False
                        cap.last_checked = result.checked_at
                        await self.db.upsert_capability(cap)

        elif pid == "local":
            local_model_ids = [m.model_id for m in result.models if m.available]
            base_url = result.base_url or f"{self._effective_local_root()}/v1"
            local_containers = [
                c for c in containers_by_id.values() if infer_service_provider(c) == "local"
            ]
            for local in local_containers:
                changed = False
                if local_model_ids and local.model not in local_model_ids:
                    local.model = local_model_ids[0]
                    changed = True
                if local.base_url != base_url:
                    local.base_url = base_url
                    changed = True
                if changed:
                    local.version += 1
                local.active = bool(local_model_ids)
                updated_containers.append(local)

            primary_local = containers_by_id.get("local_triage") or (local_containers[0] if local_containers else None)
            if local_model_ids:
                for m in result.models:
                    await self.db.upsert_capability(
                        EndpointCapability(
                            provider="api",
                            model_name=m.model_id,
                            cost_tier="local_free",
                            max_context_tokens=m.context_window,
                            strengths=primary_local.strengths if primary_local else ["triage", "classification"],
                            recommended_roles=[primary_local.role] if primary_local else ["triage"],
                            healthy=m.available,
                            available=m.available,
                            service_provider="local",
                            display_name=m.display_name,
                            is_llm=m.is_llm,
                            discovery_source=m.discovery_source,
                            last_checked=result.checked_at,
                        )
                    )
            elif primary_local is not None:
                await self.db.upsert_capability(
                    EndpointCapability(
                        provider="api",
                        model_name=primary_local.model or "local",
                        cost_tier="local_free",
                        strengths=primary_local.strengths,
                        recommended_roles=[primary_local.role],
                        healthy=False,
                        available=False,
                        service_provider="local",
                        is_llm=True,
                        discovery_source="live",
                        last_checked=result.checked_at,
                    )
                )
            for cap in await self.db.list_capabilities():
                if (
                    cap.provider == "api"
                    and cap.cost_tier == "local_free"
                    and cap.model_name not in local_model_ids
                ):
                    cap.healthy = False
                    cap.available = False
                    cap.service_provider = "local"
                    cap.last_checked = result.checked_at
                    await self.db.upsert_capability(cap)

        elif pid in _CLI_BASELINES:
            baseline_model, baseline_ctx, baseline_strengths, baseline_weaknesses = _CLI_BASELINES[pid]
            cli_authed = result.status == "authenticated"
            matching_containers = [
                c for c in containers_by_id.values() if infer_service_provider(c) == pid
            ]
            for c in matching_containers:
                if (
                    cli_authed
                    and result.discovery_status == "ok"
                    and avail_llms
                    and c.model
                    and match_available_model(pid, c.model, avail_llms) is None
                ):
                    replacement = (
                        _pick_lighter_available_model(pid, c.model, avail_llms)
                        if "fast" in c.role.lower()
                        else avail_llms[0]
                    )
                    if replacement is not None:
                        c.model = replacement.model_id
                        c.version += 1
                c.active = cli_authed
                updated_containers.append(c)

                model_name = c.model or "default"
                matched = match_available_model(pid, model_name, result.models)
                cap_available = matched.available if matched is not None else cli_authed
                cap = EndpointCapability(
                    provider=c.provider,
                    model_name=model_name,
                    cost_tier="flat_subscription",
                    max_context_tokens=(
                        matched.context_window
                        if matched and matched.context_window > 0
                        else (baseline_ctx if model_name == baseline_model else 0)
                    ),
                    strengths=list(dict.fromkeys(c.strengths + baseline_strengths)),
                    weaknesses=baseline_weaknesses + ["Context window is an estimate; verify current provider limits"],
                    recommended_roles=[c.role],
                    healthy=cli_authed and cap_available,
                    available=cli_authed and cap_available,
                    service_provider=pid,
                    display_name=matched.display_name if matched else None,
                    is_llm=True,
                    discovery_source=matched.discovery_source if matched else "static",
                    version=result.version,
                    last_checked=result.checked_at,
                )
                prev = previous_capabilities.get((cap.provider, cap.model_name))
                if prev:
                    cap.max_context_tokens = max(prev.max_context_tokens, cap.max_context_tokens)
                    cap.strengths = list(dict.fromkeys(cap.strengths + prev.strengths))
                    cap.weaknesses = prev.weaknesses.copy()
                self._apply_research(cap)
                await self.db.upsert_capability(cap)

            # Also record discovered models in endpoint_capabilities when not already written above.
            written_names = {(c.model or "default") for c in matching_containers}
            for m in result.models:
                if m.model_id in written_names or (m.display_name and m.display_name in written_names):
                    continue
                cap = EndpointCapability(
                    provider=pid,
                    model_name=m.model_id,
                    cost_tier="flat_subscription",
                    max_context_tokens=m.context_window or baseline_ctx,
                    strengths=list(baseline_strengths),
                    weaknesses=list(baseline_weaknesses),
                    recommended_roles=[],
                    healthy=m.available,
                    available=m.available,
                    service_provider=pid,
                    display_name=m.display_name,
                    is_llm=m.is_llm,
                    discovery_source=m.discovery_source,
                    version=result.version,
                    last_checked=result.checked_at,
                )
                prev = previous_capabilities.get((cap.provider, cap.model_name))
                if prev:
                    cap.max_context_tokens = max(prev.max_context_tokens, cap.max_context_tokens)
                    cap.strengths = list(dict.fromkeys(cap.strengths + prev.strengths))
                    cap.weaknesses = prev.weaknesses.copy()
                self._apply_research(cap)
                await self.db.upsert_capability(cap)

        if export_updated:
            for c in updated_containers:
                await self.db.upsert_container(c)
                self._export(c)
        return updated_containers

    async def refresh_provider(self, provider_id: str) -> DiscoveryResult:
        """Discover models & auth status for a single service provider and persist results."""
        pid = provider_id.strip().lower()
        if pid not in SERVICE_PROVIDER_BY_ID:
            raise ValueError(f"Unknown service provider: {provider_id}")
        async with self._run_lock:
            try:
                vault_creds = load_vault_credentials()
            except Exception:
                vault_creds = {}
            if not self.tavily_api_key and vault_creds.get("TAVILY_API_KEY"):
                self.tavily_api_key = vault_creds["TAVILY_API_KEY"]
            containers_by_id = {
                item.container_id: item.model_copy(deep=True)
                for item in await self.db.list_containers()
            }
            previous_capabilities = {
                (item.provider, item.model_name): item
                for item in await self.db.list_capabilities()
            }
            result = await self._discover_for_provider(pid, vault_creds=vault_creds)
            await self._sync_provider_discovery(
                result,
                containers_by_id=containers_by_id,
                previous_capabilities=previous_capabilities,
                vault_creds=vault_creds,
                export_updated=True,
            )
            return result

    async def refresh_catalog(self) -> list[AgentContainer]:
        current_task = asyncio.current_task()
        if current_task:
            self._scheduled_tasks.add(current_task)
        try:
            async with self._run_lock:
                return await self._refresh_catalog()
        finally:
            if current_task:
                self._scheduled_tasks.discard(current_task)

    async def _refresh_catalog(self) -> list[AgentContainer]:
        try:
            vault_creds = load_vault_credentials()
        except Exception:
            vault_creds = {}
        if not self.tavily_api_key and vault_creds.get("TAVILY_API_KEY"):
            self.tavily_api_key = vault_creds["TAVILY_API_KEY"]

        with self.seed_path.open(encoding="utf-8") as file:
            document = yaml.safe_load(file)
        if not isinstance(document, dict) or not isinstance(document.get("containers"), list):
            raise ValueError("Seed YAML must contain a containers list")
        seeds = [AgentContainer.model_validate(item) for item in document["containers"]]
        if self.seed_path == DEFAULT_SEED_PATH:
            seed_ids = {s.container_id for s in seeds}
            for agy_seed in (
                AgentContainer(
                    container_id="fast_auditor_agy",
                    name="Fast Auditor (Agy)",
                    role="fast_auditor",
                    provider="agy",
                    model="Gemini 4 Hydrogen (Low)",
                    cost_tier="flat_subscription",
                    allowed_tools=["read", "search"],
                    strengths=["review", "analysis", "triage"],
                    fallback_providers=["architect_claude", "tdd_engineer_codex"],
                ),
                AgentContainer(
                    container_id="repo_auditor_agy",
                    name="Repo Auditor (Agy)",
                    role="repo_auditor",
                    provider="agy",
                    model="Gemini 4 Neon (High)",
                    cost_tier="flat_subscription",
                    allowed_tools=["read", "search"],
                    strengths=["review", "analysis", "architecture"],
                    fallback_providers=["architect_claude", "tdd_engineer_codex"],
                ),
                AgentContainer(
                    container_id="agy_swarm_squad",
                    name="Agy Swarm Squad",
                    role="swarm_squad",
                    provider="agy",
                    model="Gemini 4 Neon (High)",
                    cost_tier="flat_subscription",
                    allowed_tools=["read", "search", "write", "edit"],
                    strengths=["swarm", "fullstack", "architecture", "coding", "testing"],
                    fallback_providers=["architect_claude", "tdd_engineer_codex"],
                ),
            ):
                if agy_seed.container_id not in seed_ids:
                    seeds.append(agy_seed)

        existing = {item.container_id: item for item in await self.db.list_containers()}
        previous_capabilities = {
            (item.provider, item.model_name): item for item in await self.db.list_capabilities()
        }
        containers_by_id: dict[str, AgentContainer] = {
            seed.container_id: existing.get(seed.container_id, seed).model_copy(deep=True)
            for seed in seeds
        }
        # Preserve any user-created / previously registered containers already in DB.
        for cid, existing_container in existing.items():
            if cid not in containers_by_id:
                containers_by_id[cid] = existing_container.model_copy(deep=True)

        cli_probe_overridden = "_probe_cli" in self.__dict__
        if cli_probe_overridden:
            # Backward compatibility for tests monkeypatching generator._probe_cli directly.
            cli_containers = [c for c in containers_by_id.values() if c.provider in _CLI_BASELINES]
            non_cli_ids = [
                meta["provider_id"]
                for meta in SERVICE_PROVIDER_METADATA
                if meta["provider_id"] not in _CLI_BASELINES
            ]
            cli_caps, non_cli_results, self.research_findings = await asyncio.gather(
                asyncio.gather(*(self._probe_cli(c) for c in cli_containers)),
                asyncio.gather(
                    *(self._discover_for_provider(pid, vault_creds=vault_creds) for pid in non_cli_ids)
                ),
                self.discover_web(),
            )
            cli_health: dict[str, bool] = {}
            for capability in cli_caps:
                cli_health[capability.provider] = capability.healthy
                previous = previous_capabilities.get((capability.provider, capability.model_name))
                if previous:
                    capability.max_context_tokens = previous.max_context_tokens
                    capability.strengths = list(dict.fromkeys(capability.strengths + previous.strengths))
                    capability.weaknesses = previous.weaknesses.copy()
                self._apply_research(capability)
                await self.db.upsert_capability(capability)
            for c in cli_containers:
                c.active = bool(cli_health.get(c.provider, False))
            for result in non_cli_results:
                await self._sync_provider_discovery(
                    result,
                    containers_by_id=containers_by_id,
                    previous_capabilities=previous_capabilities,
                    vault_creds=vault_creds,
                    export_updated=False,
                )
        else:
            provider_ids = [meta["provider_id"] for meta in SERVICE_PROVIDER_METADATA]
            discovery_results, self.research_findings = await asyncio.gather(
                asyncio.gather(
                    *(self._discover_for_provider(pid, vault_creds=vault_creds) for pid in provider_ids)
                ),
                self.discover_web(),
            )
            for result in discovery_results:
                await self._sync_provider_discovery(
                    result,
                    containers_by_id=containers_by_id,
                    previous_capabilities=previous_capabilities,
                    vault_creds=vault_creds,
                    export_updated=False,
                )

        for container in containers_by_id.values():
            await self.db.upsert_container(container)
            self._export(container)
        return await self.db.list_containers()

    async def run(self) -> list[AgentContainer]:
        return await self.refresh_catalog()

    @staticmethod
    def _rolodex_entry(
        container: AgentContainer,
        *,
        downshift_suggestion: str | None = None,
    ) -> dict[str, Any]:
        cost_rank = _COST_RANK_BY_TIER.get(container.cost_tier, 2)
        skills = list(container.strengths[:4]) if container.strengths else [container.role]
        skills_str = ",".join(skills)
        model_label = container.model or "default"
        line = f"{container.container_id} | {container.provider}:{model_label} | {container.cost_tier} | r{cost_rank} | {skills_str}"
        entry: dict[str, Any] = {
            "container_id": container.container_id,
            "provider": container.provider,
            "service_provider": infer_service_provider(container),
            "model": model_label,
            "role": container.role,
            "cost_tier": container.cost_tier,
            "cost_rank": cost_rank,
            "skills": skills,
            "active": container.active,
            "line": line,
        }
        if downshift_suggestion is not None:
            entry["downshift_suggestion"] = downshift_suggestion
        return entry

    async def get_rolodex(self, active_only: bool = True) -> list[dict]:
        containers = await self.db.list_containers()
        models_by_provider: dict[str, list[AvailableModel]] = {}
        if hasattr(self.db, "list_provider_models"):
            for item in await self.db.list_provider_models(available_only=True, llm_only=True):
                models_by_provider.setdefault(item.provider_id, []).append(item)

        entries = []
        for c in containers:
            if active_only and not c.active:
                continue
            sp = infer_service_provider(c)
            avail_llms = models_by_provider.get(sp, [])
            suggestion: str | None = None
            if avail_llms:
                lighter = _pick_lighter_available_model(sp, c.model, avail_llms)
                current_match = match_available_model(sp, c.model, avail_llms)
                current_id = current_match.model_id.lower() if current_match else (c.model or "").lower()
                if lighter is not None and lighter.model_id.lower() != current_id:
                    suggestion = lighter.model_id
            entries.append(self._rolodex_entry(c, downshift_suggestion=suggestion))
        entries.sort(key=lambda e: (e["cost_rank"], e["container_id"]))
        return entries

    @staticmethod
    def generate_rolodex_text(
        entries: list[dict] | None = None,
        *,
        active_only: bool = True,
    ) -> str:
        if not entries:
            return ""
        filtered = [e for e in entries if not active_only or e.get("active", True)]
        if not filtered:
            return ""
        header = "container_id | provider:model | tier | cost_rank | skills"
        lines = [e["line"] for e in filtered if "line" in e]
        return "\n".join([header, *lines])

    async def downshift_container(
        self,
        container_id: str,
        target_model: str | None = None,
        target_container_id: str | None = None,
    ) -> AgentContainer:
        containers = {c.container_id: c for c in await self.db.list_containers()}
        container = containers.get(container_id)
        if container is None:
            raise ValueError(f"unknown container: {container_id}")
        updates: dict[str, Any] = {"version": container.version + 1}
        new_model = target_model
        effective_container = container
        if target_container_id and target_container_id in containers:
            target = containers[target_container_id]
            effective_container = target
            if not new_model:
                new_model = target.model
            if (
                target.provider != container.provider
                or target.base_url != container.base_url
            ):
                updates["provider"] = target.provider
                updates["cost_tier"] = target.cost_tier
                updates["base_url"] = target.base_url
                updates["api_key_env"] = target.api_key_env

        service_provider = infer_service_provider(effective_container)
        available_llms: list[AvailableModel] = []
        if hasattr(self.db, "list_provider_models"):
            available_llms = await self.db.list_provider_models(
                service_provider, available_only=True, llm_only=True
            )

        if target_model is not None and target_model.strip():
            if available_llms:
                matched = match_available_model(service_provider, target_model, available_llms)
                if matched is None:
                    raise ValueError(
                        f"Model {target_model!r} is not available for provider {service_provider!r}"
                    )
                new_model = matched.model_id
            else:
                new_model = target_model.strip()
        elif not new_model:
            if available_llms:
                picked = _pick_lighter_available_model(
                    service_provider, effective_container.model, available_llms
                )
                assert picked is not None
                new_model = picked.model_id
            elif service_provider == "local":
                if effective_container.model:
                    new_model = effective_container.model
                else:
                    raise ValueError("No available models discovered for provider 'local'")
            else:
                new_model = _DOWNSHIFT_DEFAULTS.get(service_provider, "gpt-4o-mini")

        updates["model"] = new_model
        updated = container.model_copy(update=updates)
        await self.db.upsert_container(updated)
        self._export(updated)
        return updated

    def _export(self, container: AgentContainer) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", container.container_id):
            raise ValueError("container_id must be safe for a filename")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        target = self.export_dir / f"{container.container_id}.v{container.version}.yaml"
        staging = self.export_dir / f".{target.name}.{uuid4().hex}.tmp"
        try:
            with staging.open("w", encoding="utf-8") as file:
                yaml.safe_dump(container.model_dump(mode="json"), file, sort_keys=False, allow_unicode=True)
            staging.replace(target)
        finally:
            staging.unlink(missing_ok=True)


GeneratorAgent = AgentGenerator
