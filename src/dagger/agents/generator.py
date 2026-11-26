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
from typing import TYPE_CHECKING
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
import yaml
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL

from dagger.schema import AgentContainer, EndpointCapability

if TYPE_CHECKING:
    from dagger.db import DatabaseManager

logger = logging.getLogger(__name__)
DEFAULT_SEED_PATH = Path(__file__).resolve().parents[3] / "containers" / "seed_containers.yaml"
_GENERATORS: dict[str, "AgentGenerator"] = {}
_AUTH_COMMANDS = {
    "claude": ("auth", "status"),
    "codex": ("login", "status"),
    "gemini": ("auth", "status"),
    "copilot": ("auth", "status"),
}
_API_KEYS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "GROQ_API_KEY", "TAVILY_API_KEY",
    "DAGGER_API_KEY", "DAGGER_API_BASE_URL",
)
_DOWNSHIFT_DEFAULTS: dict[str, str] = {
    "claude": "claude-haiku-4-5",
    "codex": "gpt-4.1-mini",
    "gemini": "gemini-2.5-flash",
    "copilot": "gpt-4.1-mini",
    "groq": "llama-3.1-8b-instant",
    "api": "gpt-4o-mini",
    "agy": "gemini-4-hydrogen-low",
}
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
    "agy": ("gemini-4-hydrogen", 1_000_000, ["review", "analysis"], ["Requires Antigravity CLI"]),
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


async def _run_scheduled_generator(key: str) -> None:
    generator = _GENERATORS.get(key)
    if generator is None:
        logger.warning("No generator registered for scheduled job %s", key)
        return
    await generator.refresh_catalog()


def _has_gemini_auth() -> bool:
    """Return True when Gemini CLI has valid OAuth/settings or API/Vertex/GCA env credentials."""
    if os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip():
        return True
    for flag in ("GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_GENAI_USE_GCA"):
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
    selected = settings.get("selectedType") or (auth.get("selectedType") if isinstance(auth, dict) else None)
    if not selected:
        return False
    if selected == "oauth-personal":
        return (gemini_dir / "oauth_creds.json").is_file()
    return True


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
        normalized = status.lower()
        try:
            details = json.loads(status)
        except (ValueError, TypeError):
            details = None
        denied = isinstance(details, dict) and any(
            details.get(field) is False for field in ("loggedIn", "logged_in", "authenticated")
        )
        return (
            auth_ok
            and not any(
                marker in normalized
                for marker in ("not logged in", "not authenticated", "unauthenticated", "logged out")
            )
            and not denied
        )

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
            else:
                auth_ok, status = await self._command(executable, *_AUTH_COMMANDS[container.provider])
                cli_healthy = self._is_auth_output_healthy(auth_ok, status)
                if container.provider == "copilot" and not cli_healthy:
                    gh_exe = shutil.which("gh")
                    if gh_exe:
                        gh_ok, gh_status = await self._command(gh_exe, "auth", "status")
                        if self._is_auth_output_healthy(gh_ok, gh_status):
                            cli_healthy = True
                if container.provider == "gemini":
                    healthy = version_ok and _has_gemini_auth()
                else:
                    healthy = version_ok and cli_healthy
        elif container.provider == "copilot":
            gh_exe = shutil.which("gh")
            if gh_exe:
                gh_ok, gh_status = await self._command(gh_exe, "auth", "status")
                healthy = self._is_auth_output_healthy(gh_ok, gh_status)
        baseline_model, context, strengths, weaknesses = _CLI_BASELINES[container.provider]
        return EndpointCapability(
            provider=container.provider, model_name=container.model or "default", cost_tier="flat_subscription",
            max_context_tokens=context if container.model == baseline_model else 0,
            strengths=list(dict.fromkeys(container.strengths + strengths)),
            weaknesses=weaknesses + ["Context window is an estimate; verify current provider limits"],
            recommended_roles=[container.role],
            healthy=healthy, version=version, last_checked=datetime.now(timezone.utc),
        )

    async def _probe_local(self) -> list[str]:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(f"{self.local_base_url}/v1/models")
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
            mentioned = {provider for provider in _AUTH_COMMANDS if re.search(
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
            from dagger.vault import load_vault_credentials
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
        containers = [existing.get(seed.container_id, seed).model_copy(deep=True) for seed in seeds]
        container_ids = {c.container_id for c in containers}

        if vault_creds.get("GROQ_API_KEY") and "groq_fast_router" not in container_ids:
            groq_container = existing.get(
                "groq_fast_router",
                AgentContainer(
                    container_id="groq_fast_router",
                    name="Groq Fast Router",
                    provider="api",
                    base_url="https://api.groq.com/openai/v1",
                    model="llama-3.3-70b-versatile",
                    role="fast_router",
                    cost_tier="metered_api",
                    allowed_tools=["read", "search"],
                    strengths=["triage", "routing", "review", "analysis"],
                    active=True,
                ),
            ).model_copy(deep=True)
            containers.append(groq_container)
            container_ids.add("groq_fast_router")
            await self.db.upsert_capability(EndpointCapability(
                provider="api",
                model_name=groq_container.model,
                cost_tier="metered_api",
                strengths=groq_container.strengths,
                recommended_roles=[groq_container.role],
                healthy=True,
                last_checked=datetime.now(timezone.utc),
            ))

        if vault_creds.get("TAVILY_API_KEY") and "tavily_researcher" not in container_ids:
            tavily_container = existing.get(
                "tavily_researcher",
                AgentContainer(
                    container_id="tavily_researcher",
                    name="Tavily Researcher",
                    provider="api",
                    model="tavily-search",
                    role="researcher",
                    cost_tier="metered_api",
                    allowed_tools=["search"],
                    strengths=["web_search", "research", "discovery"],
                    active=True,
                ),
            ).model_copy(deep=True)
            containers.append(tavily_container)
            container_ids.add("tavily_researcher")
            await self.db.upsert_capability(EndpointCapability(
                provider="api",
                model_name=tavily_container.model,
                cost_tier="metered_api",
                strengths=tavily_container.strengths,
                recommended_roles=[tavily_container.role],
                healthy=True,
                last_checked=datetime.now(timezone.utc),
            ))

        if vault_creds.get("DAGGER_API_KEY") and "custom_api_agent" not in container_ids:
            custom_container = existing.get(
                "custom_api_agent",
                AgentContainer(
                    container_id="custom_api_agent",
                    name="Custom API Agent",
                    provider="api",
                    model=os.getenv("DAGGER_API_MODEL", "gpt-4o-mini"),
                    base_url=vault_creds.get("DAGGER_API_BASE_URL"),
                    role="worker",
                    cost_tier="metered_api",
                    allowed_tools=["read", "search", "write", "edit"],
                    strengths=["coding", "analysis"],
                    active=True,
                ),
            ).model_copy(deep=True)
            containers.append(custom_container)
            container_ids.add("custom_api_agent")
            await self.db.upsert_capability(EndpointCapability(
                provider="api",
                model_name=custom_container.model,
                cost_tier="metered_api",
                strengths=custom_container.strengths,
                recommended_roles=[custom_container.role],
                healthy=True,
                last_checked=datetime.now(timezone.utc),
            ))
        cli_containers = [item for item in containers if item.provider in _CLI_BASELINES]
        cli_capabilities, models, self.research_findings = await asyncio.gather(
            asyncio.gather(*(self._probe_cli(item) for item in cli_containers)),
            self._probe_local(), self.discover_web(),
        )
        cli_health: dict[str, bool] = {}
        for capability in cli_capabilities:
            cli_health[capability.provider] = capability.healthy
            previous = previous_capabilities.get((capability.provider, capability.model_name))
            if previous:
                capability.max_context_tokens = previous.max_context_tokens
                capability.strengths = list(dict.fromkeys(capability.strengths + previous.strengths))
                capability.weaknesses = previous.weaknesses.copy()
            self._apply_research(capability)
            await self.db.upsert_capability(capability)
        local = next((item for item in containers if item.container_id == "local_triage"), None)
        if local:
            changed = False
            if models and local.model not in models:
                local.model = models[0]
                changed = True
            base_url = f"{self.local_base_url}/v1"
            if local.base_url != base_url:
                local.base_url = base_url
                changed = True
            if changed:
                local.version += 1
            for model in models or [local.model or "local"]:
                await self.db.upsert_capability(EndpointCapability(
                    provider="api", model_name=model, cost_tier="local_free",
                    strengths=local.strengths, recommended_roles=[local.role],
                    healthy=model in models,
                ))
            for capability in await self.db.list_capabilities():
                if capability.provider == "api" and capability.cost_tier == "local_free" and capability.model_name not in models:
                    capability.healthy = False
                    capability.last_checked = datetime.now(timezone.utc)
                    await self.db.upsert_capability(capability)
        for container in containers:
            if container.cost_tier == "local_free":
                container.active = bool(models)
            elif container.provider in _CLI_BASELINES:
                container.active = bool(cli_health.get(container.provider, False))
            elif container.provider in ("api", "groq", "tavily"):
                cid = container.container_id.lower()
                burl = (container.base_url or "").lower()
                if container.provider == "groq" or "groq" in cid or "groq.com" in burl:
                    req_key = "GROQ_API_KEY"
                elif container.provider == "tavily" or "tavily" in cid:
                    req_key = "TAVILY_API_KEY"
                else:
                    req_key = "DAGGER_API_KEY"
                container.active = bool(vault_creds.get(req_key) or os.environ.get(req_key, "").strip())
            await self.db.upsert_container(container)
            self._export(container)
        return await self.db.list_containers()

    async def run(self) -> list[AgentContainer]:
        return await self.refresh_catalog()

    @staticmethod
    def _rolodex_entry(container: AgentContainer) -> dict:
        cost_rank = _COST_RANK_BY_TIER.get(container.cost_tier, 2)
        skills = list(container.strengths[:4]) if container.strengths else [container.role]
        skills_str = ",".join(skills)
        model_label = container.model or "default"
        line = f"{container.container_id} | {container.provider}:{model_label} | {container.cost_tier} | r{cost_rank} | {skills_str}"
        return {
            "container_id": container.container_id,
            "provider": container.provider,
            "model": model_label,
            "role": container.role,
            "cost_tier": container.cost_tier,
            "cost_rank": cost_rank,
            "skills": skills,
            "active": container.active,
            "line": line,
        }

    async def get_rolodex(self, active_only: bool = True) -> list[dict]:
        containers = await self.db.list_containers()
        entries = [
            self._rolodex_entry(c)
            for c in containers
            if not active_only or c.active
        ]
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
        updates: dict = {"version": container.version + 1}
        new_model = target_model
        if target_container_id and target_container_id in containers:
            target = containers[target_container_id]
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
        if not new_model:
            is_groq = container.provider == "groq" or bool(
                container.base_url and "groq.com" in container.base_url
            )
            default_key = "groq" if is_groq else container.provider
            new_model = _DOWNSHIFT_DEFAULTS.get(default_key, "gpt-4o-mini")
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
