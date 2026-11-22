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
    "GITHUB_TOKEN", "GH_TOKEN",
)
_CLI_BASELINES = {
    "claude": ("claude-sonnet-4-5", 200_000, ["architecture", "reasoning"], ["Requires Claude CLI authentication"]),
    "codex": ("gpt-5-codex", 128_000, ["coding", "testing"], ["Requires Codex CLI authentication"]),
    "gemini": ("gemini-2.5-pro", 1_000_000, ["analysis", "review"], ["Requires Gemini CLI authentication"]),
    "copilot": ("gpt-4.1", 128_000, ["coding", "patching"], ["Requires Copilot CLI authentication"]),
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

    async def _probe_cli(self, container: AgentContainer) -> EndpointCapability:
        executable = shutil.which(container.provider)
        version = None
        healthy = False
        if executable:
            version_ok, output = await self._command(executable, "--version")
            if version_ok and output:
                version = output.splitlines()[0][:200]
            auth_ok, status = await self._command(executable, *_AUTH_COMMANDS[container.provider])
            normalized = status.lower()
            try:
                details = json.loads(status)
            except (ValueError, TypeError):
                details = None
            denied = isinstance(details, dict) and any(
                details.get(field) is False for field in ("loggedIn", "logged_in", "authenticated")
            )
            healthy = version_ok and auth_ok and not any(
                marker in normalized for marker in ("not logged in", "not authenticated", "unauthenticated", "logged out")
            ) and not denied
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
                number = float(match.group(1).replace(",", ""))
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
        with self.seed_path.open(encoding="utf-8") as file:
            document = yaml.safe_load(file)
        if not isinstance(document, dict) or not isinstance(document.get("containers"), list):
            raise ValueError("Seed YAML must contain a containers list")
        seeds = [AgentContainer.model_validate(item) for item in document["containers"]]
        existing = {item.container_id: item for item in await self.db.list_containers()}
        previous_capabilities = {
            (item.provider, item.model_name): item for item in await self.db.list_capabilities()
        }
        containers = [existing.get(seed.container_id, seed).model_copy(deep=True) for seed in seeds]
        cli_containers = [item for item in containers if item.provider in _AUTH_COMMANDS]
        cli_capabilities, models, self.research_findings = await asyncio.gather(
            asyncio.gather(*(self._probe_cli(item) for item in cli_containers)),
            self._probe_local(), self.discover_web(),
        )
        for capability in cli_capabilities:
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
            await self.db.upsert_container(container)
            self._export(container)
        return await self.db.list_containers()

    async def run(self) -> list[AgentContainer]:
        return await self.refresh_catalog()

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
