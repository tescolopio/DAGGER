"""Local control-plane API and graph event stream."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import httpx
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, ValidationError
from watchfiles import awatch

from dagger import paths
from dagger.agents.generator import AgentGenerator
from dagger.agents.orchestrator import Orchestrator
from dagger.agents.project_manager import ProjectManager
from dagger.bus import EventBus
from dagger.db import DatabaseManager
from dagger.schema import (
    AgentContainer,
    AvailableModel,
    DefinitionOfDone,
    KanbanTask,
    ProviderCredentialStatus,
    TaskEnvelope,
)
from dagger.providers import ProviderError, ProviderRegistry
from dagger.providers.cli_runners import AgyCliRunner
from dagger.providers.discovery import (
    CLI_PROBE_STRIPPED_ENV,
    SERVICE_PROVIDER_BY_ID,
    SERVICE_PROVIDER_METADATA,
    DiscoveryResult,
    discover_provider_models,
    has_copilot_local_auth,
    has_gemini_subscription_auth,
)
from dagger.vault import get_base_url, get_vault_credential, save_provider_credentials

logger = logging.getLogger(__name__)


class LoginCompleteRequest(BaseModel):
    input: str = ""


def _trusted_websocket_origin(websocket: WebSocket) -> bool:
    origin = websocket.headers.get("origin")
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme in {"http", "https"}
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and parsed.username is None
            and parsed.password is None
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


class RunRequest(BaseModel):
    goal: str
    run_id: str | None = None


class ReviewRequest(BaseModel):
    decision: bool | str | dict[str, Any]


class CredentialsRequest(BaseModel):
    provider: str
    api_key: str = ""
    base_url: str | None = None


class DownshiftRequest(BaseModel):
    target_model: str | None = None
    target_container_id: str | None = None


_PM_ROLE_PRIORITY: dict[str, int] = {
    "architect": 0,
    "planner": 0,
    "project_manager": 0,
    "repo_auditor": 1,
    "auditor": 1,
    "engineer": 2,
    "developer": 2,
    "worker": 2,
    "patcher": 2,
    "coder": 2,
    "fast_auditor": 3,
    "fast_router": 4,
    "triage": 4,
    "researcher": 5,
}
_PM_COST_PRIORITY: dict[str, int] = {
    "flat_subscription": 0,
    "local_free": 1,
    "metered_api": 2,
}
_PM_LIGHTWEIGHT_MODEL_MARKERS = ("flash", "hydrogen", "mini", "haiku", "lite", "nano")


def _is_swarm_container(container: AgentContainer) -> bool:
    return container.container_id == "agy_swarm_squad" or container.role == "swarm_squad"


def _infer_service_provider(container: AgentContainer) -> str:
    try:
        from dagger.agents.generator import infer_service_provider

        return infer_service_provider(container)
    except ImportError:
        pass
    if container.cost_tier == "local_free":
        return "local"
    if container.provider in ("claude", "codex", "gemini", "copilot", "agy"):
        return container.provider
    cid = container.container_id.lower()
    burl = (container.base_url or "").lower()
    model = (container.model or "").lower()
    if "tavily" in cid or model == "tavily-search":
        return "tavily"
    if "groq" in cid or "groq.com" in burl:
        return "groq"
    return "api"


def _is_non_llm_container(container: AgentContainer, capabilities: list[Any]) -> bool:
    service_provider = _infer_service_provider(container)
    if (
        container.container_id == "tavily_researcher"
        or service_provider == "tavily"
        or (container.model or "").strip().lower() == "tavily-search"
    ):
        return True
    matching_caps = [
        item
        for item in capabilities
        if item.provider == container.provider
        and (container.model is None or item.model_name == container.model)
    ]
    if matching_caps and all(getattr(item, "is_llm", True) is False for item in matching_caps):
        return True
    return False


def _model_matches_available(
    service_provider: str, container_model: str | None, model: AvailableModel
) -> bool:
    if not container_model:
        return True
    try:
        from dagger.agents.generator import match_available_model

        return match_available_model(service_provider, container_model, [model]) is not None
    except ImportError:
        pass
    raw = container_model.strip().lower()
    if raw == model.model_id.strip().lower():
        return True
    if model.display_name and raw == model.display_name.strip().lower():
        return True
    if service_provider == "agy":
        known_aliases = {
            label.strip().lower(): slug.strip().lower()
            for slug, label in AgyCliRunner.SUPPORTED_MODEL_SPECS
        }
        if known_aliases.get(raw) == model.model_id.strip().lower():
            return True
    return False


def _pm_container_sort_key(container: AgentContainer) -> tuple[int, int, int, str]:
    role = container.role.casefold()
    if role in _PM_ROLE_PRIORITY:
        role_rank = _PM_ROLE_PRIORITY[role]
    elif "architect" in role or "plan" in role:
        role_rank = 0
    elif "research" in role:
        role_rank = 5
    elif "triage" in role or "router" in role:
        role_rank = 4
    elif "fast" in role:
        role_rank = 3
    elif "auditor" in role:
        role_rank = 1
    else:
        role_rank = 2
    model_lower = (container.model or "").casefold()
    is_lightweight = container.cost_tier == "local_free" or any(
        marker in model_lower for marker in _PM_LIGHTWEIGHT_MODEL_MARKERS
    )
    return (
        role_rank,
        _PM_COST_PRIORITY.get(container.cost_tier, 3),
        1 if is_lightweight else 0,
        container.container_id,
    )


def _select_pm_containers(
    containers: list[AgentContainer],
    capabilities: list[Any],
    provider_models: list[AvailableModel] | None = None,
) -> list[AgentContainer]:
    """Return ranked active+healthy non-swarm LLM containers suitable for PM planning."""
    healthy = {
        (item.provider, item.model_name)
        for item in capabilities
        if item.healthy
        and getattr(item, "available", True)
        and getattr(item, "is_llm", True)
    }
    discovered_by_provider: dict[str, list[AvailableModel]] = {}
    for model in provider_models or []:
        discovered_by_provider.setdefault(model.provider_id.strip().lower(), []).append(model)

    eligible: list[tuple[int, AgentContainer]] = []
    for item in containers:
        if not item.active or _is_swarm_container(item) or _is_non_llm_container(item, capabilities):
            continue
        if not any(
            provider == item.provider and (item.model is None or model == item.model)
            for provider, model in healthy
        ):
            continue
        service_provider = _infer_service_provider(item)
        sp_models = discovered_by_provider.get(service_provider, [])
        availability_rank = 0
        if sp_models:
            matched = [
                m for m in sp_models if _model_matches_available(service_provider, item.model, m)
            ]
            if matched and all(not m.is_llm for m in matched):
                continue
            if any(m.available and m.is_llm for m in matched):
                availability_rank = 0
            else:
                availability_rank = 1
        eligible.append((availability_rank, item))

    eligible.sort(key=lambda pair: (pair[0], _pm_container_sort_key(pair[1])))
    return [container for _, container in eligible]


def create_app(
    db_path: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    *,
    generator_factory: Any = AgentGenerator,
    container_dir: str | Path | None = None,
    project: str | Path | None = None,
) -> FastAPI:
    """Create an app, optionally overriding storage paths for an isolated instance.

    The project (the repository agents work on) defaults to DAGGER_PROJECT or the git root
    of the current directory; its state lives outside the project (see dagger.paths).
    """
    project_root = paths.resolve_project(project)
    data_path = paths.project_state_dir(project_root)
    if container_dir is not None:
        writable_definitions = Path(container_dir)
        bundled_definitions: Path | None = None
    else:
        writable_definitions = paths.writable_containers_dir(project_root)
        bundled_definitions = paths.bundled_containers_dir()
        if bundled_definitions.resolve() == writable_definitions.resolve():
            bundled_definitions = None
    skill_roots = paths.bundled_skill_roots(project_root)
    database_path = Path(db_path) if db_path is not None else data_path / "dagger.sqlite3"
    if db_path is not None:
        data_path = database_path.parent
    checkpoint_file = (
        Path(checkpoint_path) if checkpoint_path is not None else data_path / "checkpoints.sqlite3"
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database_path.parent.mkdir(parents=True, exist_ok=True)
        checkpoint_file.parent.mkdir(parents=True, exist_ok=True)
        db = DatabaseManager(database_path)
        await db.initialize()
        generator = None
        jobs: set[asyncio.Task] = set()
        try:
            async with AsyncSqliteSaver.from_conn_string(str(checkpoint_file)) as checkpointer:
                checkpointer.serde = JsonPlusSerializer(
                    pickle_fallback=False, allowed_msgpack_modules=None
                )
                await checkpointer.conn.execute("PRAGMA journal_mode=WAL")
                await checkpointer.conn.execute("PRAGMA foreign_keys=ON")
                async with checkpointer.conn.execute("PRAGMA integrity_check") as cursor:
                    result = await cursor.fetchone()
                if result is None or result[0] != "ok":
                    raise RuntimeError(f"Checkpoint database integrity check failed: {result}")
                bus = EventBus()
                try:
                    registry = ProviderRegistry(db=db)
                except TypeError:
                    registry = ProviderRegistry()

                async def execute(prompt: str) -> str:
                    containers = await db.list_containers()
                    capabilities = await db.list_capabilities()
                    provider_models = await db.list_provider_models()
                    candidates = _select_pm_containers(containers, capabilities, provider_models)
                    if not candidates:
                        raise RuntimeError("No available non-swarm agent container is configured for PM planning")
                    errors: list[str] = []
                    for container in candidates:
                        pm_container = container.model_copy(update={"allowed_tools": ["read", "search"]})
                        manager.provider = pm_container.provider
                        manager.model = pm_container.model
                        try:
                            result = await registry.get(pm_container.provider).run(
                                pm_container,
                                TaskEnvelope(
                                    task_id=str(uuid4()),
                                    run_id=str(uuid4()),
                                    kanban_id=str(uuid4()),
                                    prompt=prompt,
                                    container_id=pm_container.container_id,
                                    worktree_path=str(project_root),
                                    dod=DefinitionOfDone(acceptance_criteria=[]),
                                ),
                            )
                            return result if isinstance(result, str) else str(result)
                        except ProviderError as exc:
                            logger.warning(
                                "PM planner container %s (%s) failed: %s",
                                pm_container.container_id,
                                pm_container.provider,
                                exc,
                            )
                            errors.append(f"{pm_container.container_id} ({pm_container.provider}): {exc}")
                    raise RuntimeError("All PM planner containers failed: " + "; ".join(errors))

                manager = ProjectManager(planner=execute, worker=execute, workspace=project_root)
                bus_path = data_path / "bus"
                orchestrator = Orchestrator(
                    db, registry, manager, bus=bus, checkpointer=checkpointer,
                    bus_path=bus_path, worktree_path=project_root, skill_roots=skill_roots,
                )
                bus_path.mkdir(parents=True, exist_ok=True)
                definitions_path = writable_definitions
                definitions_path.mkdir(parents=True, exist_ok=True)

                async def load_definition(path: Path, *, initial: bool = False) -> None:
                    if path.is_symlink() or not path.is_file() or path.suffix.lower() not in {".yaml", ".yml"}:
                        return
                    try:
                        document = yaml.safe_load(path.read_text(encoding="utf-8"))
                        values = document.get("containers", [document]) if isinstance(document, dict) else document
                        if not isinstance(values, list):
                            raise ValueError("expected a container or list of containers")
                        containers = [AgentContainer.model_validate(value) for value in values]
                        for container in containers:
                            current = await db.get_container(container.container_id)
                            if current and (initial or container.version < current.version):
                                continue
                            await db.upsert_container(container)
                        await bus.publish({"type": "container_updated"})
                    except (OSError, ValueError, yaml.YAMLError, ValidationError):
                        logger.exception("Could not reload container definitions from %s", path)

                # The writable catalog loads first so a user's definition wins over the bundled one.
                for directory in (definitions_path, bundled_definitions):
                    if directory is not None and directory.is_dir():
                        for path in sorted(directory.iterdir()):
                            await load_definition(path, initial=True)

                generator = generator_factory(
                    db, job_store_path=data_path / "scheduler.sqlite3",
                    export_dir=definitions_path,
                )
                await generator.refresh_catalog()
                generator.start()
                app.state.db = db
                app.state.workspace_root = project_root
                app.state.skill_roots = skill_roots
                app.state.data_path = data_path
                app.state.bus = bus
                app.state.orchestrator = orchestrator
                app.state.generator = generator
                app.state.jobs = jobs
                app.state.active_runs = set()
                app.state.login_sessions = {}

                async def watch_bus() -> None:
                    async for changes in awatch(bus_path):
                        await bus.publish({"type": "file_changed", "count": len(changes)})

                async def watch_definitions() -> None:
                    async for changes in awatch(definitions_path):
                        for _, path in changes:
                            await load_definition(Path(path))

                watchers = [asyncio.create_task(watch_bus()), asyncio.create_task(watch_definitions())]
                try:
                    yield
                finally:
                    for watcher in watchers:
                        watcher.cancel()
                    for job in jobs:
                        job.cancel()
                    for session in list(app.state.login_sessions.values()):
                        await _terminate_login_session(session)
                    app.state.login_sessions.clear()
                    await asyncio.gather(*watchers, *jobs, return_exceptions=True)
                    await generator.aclose()
        finally:
            await db.close()

    app = FastAPI(title="DAGGER", lifespan=lifespan)

    def launch(coro: Any, run_id: str | None = None) -> None:
        if run_id is not None and run_id in app.state.active_runs:
            coro.close()
            raise HTTPException(status_code=409, detail="Run is already executing")
        if run_id is not None:
            app.state.active_runs.add(run_id)
        task = asyncio.create_task(coro)
        app.state.jobs.add(task)

        def finished(job: asyncio.Task) -> None:
            app.state.jobs.discard(job)
            if run_id is not None:
                app.state.active_runs.discard(run_id)
            if not job.cancelled() and job.exception() is not None:
                logger.error("Graph execution failed: %s", job.exception())

        task.add_done_callback(finished)

    @app.get("/api/graph")
    async def graph(run_id: str | None = None):
        return await app.state.orchestrator.get_graph(run_id)

    @app.get("/api/containers")
    async def containers():
        return await app.state.db.list_containers()

    @app.get("/api/health")
    async def health():
        capabilities = await app.state.db.list_capabilities()
        scheduler = getattr(app.state.generator, "scheduler", None)
        return {
            "status": "ok",
            "scheduler_running": bool(scheduler and scheduler.running),
            "providers": sorted({item.provider for item in capabilities}),
            "healthy_providers": sorted({item.provider for item in capabilities if item.healthy}),
        }

    @app.get("/api/capabilities")
    async def capabilities():
        return await app.state.db.list_capabilities()

    @app.post("/api/containers", status_code=201)
    async def create_container(container: AgentContainer):
        return await app.state.db.upsert_container(container)

    @app.post("/api/generator/refresh")
    async def refresh_catalog():
        return await app.state.generator.refresh_catalog()

    async def _refresh_single_provider(pid: str) -> DiscoveryResult | None:
        refresh_fn = getattr(app.state.generator, "refresh_provider", None)
        if callable(refresh_fn):
            result = await refresh_fn(pid)
            return result if isinstance(result, DiscoveryResult) else None
        await app.state.generator.refresh_catalog()
        return None

    async def _build_provider_status(
        pid: str,
        *,
        capabilities: list[Any] | None = None,
        models_by_provider: dict[str, list[AvailableModel]] | None = None,
        discovery_override: DiscoveryResult | None = None,
    ) -> ProviderCredentialStatus:
        meta = SERVICE_PROVIDER_BY_ID[pid]
        caps = capabilities if capabilities is not None else await app.state.db.list_capabilities()
        if models_by_provider is not None:
            db_models = models_by_provider.get(pid, [])
        else:
            db_models = await app.state.db.list_provider_models(pid)

        last_map = getattr(app.state.generator, "last_discovery", None)
        last_disc: DiscoveryResult | None = (
            discovery_override
            if discovery_override is not None
            else (last_map.get(pid) if isinstance(last_map, dict) else None)
        )

        if discovery_override is not None:
            provider_models = list(discovery_override.models)
        else:
            provider_models = db_models if db_models else (list(last_disc.models) if last_disc is not None else [])

        available_models = [item for item in provider_models if item.available]
        supports_base_url = bool(meta["supports_base_url"])
        configured_url = get_base_url(pid) if supports_base_url else None
        if pid == "local":
            base_url = (
                configured_url
                or (last_disc.base_url if last_disc and last_disc.base_url else None)
                or meta.get("default_base_url")
            )
        elif pid == "api":
            base_url = configured_url
        else:
            base_url = None

        if last_disc is not None:
            discovery_status = last_disc.discovery_status
            discovery_error = last_disc.discovery_error
            last_discovered_at = last_disc.checked_at
        elif provider_models:
            last_discovered_at = max(m.last_checked for m in provider_models)
            if any(m.discovery_source == "fallback" for m in provider_models):
                discovery_status = "fallback"
            elif available_models:
                discovery_status = "ok"
            else:
                discovery_status = "unverified"
            discovery_error = None
        else:
            discovery_status = "unverified"
            discovery_error = None
            last_discovered_at = None

        kind = meta["kind"]
        if kind == "subscription_cli":
            binary = meta["binary"]
            found = shutil.which(binary)
            prov_caps = [
                item
                for item in caps
                if (getattr(item, "service_provider", None) or item.provider) == pid
            ]
            if not found:
                status = "missing_binary"
                detail = f"{binary} binary not found in PATH"
            elif last_disc is not None:
                status = last_disc.status
                detail = last_disc.detail
            elif pid == "gemini":
                if not has_gemini_subscription_auth():
                    status = "unauthenticated"
                    detail = "gemini CLI found, but no subscription OAuth/GCA/Vertex credentials configured"
                elif prov_caps and not any(item.healthy for item in prov_caps):
                    status = "expired"
                    detail = f"{binary} found at {found}, but health check failed"
                else:
                    status = "authenticated"
                    detail = f"Found at {found}"
            elif pid == "copilot":
                if prov_caps and any(item.healthy for item in prov_caps):
                    status = "authenticated"
                    detail = f"Found at {found}"
                elif prov_caps and not any(item.healthy for item in prov_caps):
                    status = "unauthenticated"
                    detail = f"{binary} found at {found}, but not authenticated (run copilot login)"
                elif has_copilot_local_auth():
                    status = "authenticated"
                    detail = f"Found at {found}"
                else:
                    status = "unauthenticated"
                    detail = f"{binary} found at {found}, but not authenticated (run copilot login)"
            elif prov_caps and not any(item.healthy for item in prov_caps):
                status = "expired"
                detail = f"{binary} found at {found}, but health check failed"
            else:
                status = "authenticated"
                detail = f"Found at {found}"
        elif kind == "api_key":
            env_key = meta["env_key"]
            configured = bool(get_vault_credential(env_key))
            if last_disc is not None:
                if not configured and last_disc.status not in ("unauthenticated", "unconfigured"):
                    status = "unauthenticated"
                    detail = "No API key configured"
                else:
                    status = last_disc.status
                    detail = last_disc.detail
            elif configured:
                status = "configured"
                if pid == "api" and not base_url:
                    detail = "API key configured; set Base URL to discover available models"
                else:
                    detail = "Configured in local vault/env"
            else:
                status = "unauthenticated"
                detail = "No API key configured"
        else:
            if last_disc is not None:
                status = last_disc.status
                detail = last_disc.detail
            else:
                local_healthy = any(
                    (getattr(item, "service_provider", None) == "local" or item.cost_tier == "local_free")
                    and item.healthy
                    for item in caps
                ) or bool(available_models)
                status = "authenticated" if local_healthy else "unreachable"
                detail = (
                    f"Healthy local endpoint detected at {base_url}"
                    if local_healthy
                    else f"No local endpoint active at {base_url}"
                )

        return ProviderCredentialStatus(
            provider_id=pid,
            display_name=meta["display_name"],
            kind=kind,
            status=status,
            detail=detail,
            supports_login=bool(meta["supports_login"]),
            supports_api_key=bool(meta["supports_api_key"]),
            supports_base_url=supports_base_url,
            requires_api_key=bool(meta["requires_api_key"]),
            base_url=base_url,
            category=meta["category"],
            available_models=available_models,
            available_model_count=len(available_models),
            discovery_status=discovery_status,
            discovery_error=discovery_error,
            last_discovered_at=last_discovered_at,
        )

    @app.get("/api/providers/status")
    async def providers_status():
        capabilities = await app.state.db.list_capabilities()
        all_models = await app.state.db.list_provider_models()
        models_by_provider: dict[str, list[AvailableModel]] = {}
        for model in all_models:
            models_by_provider.setdefault(model.provider_id.strip().lower(), []).append(model)
        providers: list[dict[str, Any]] = []
        for meta in SERVICE_PROVIDER_METADATA:
            item = await _build_provider_status(
                meta["provider_id"],
                capabilities=capabilities,
                models_by_provider=models_by_provider,
            )
            providers.append(item.model_dump(mode="json"))
        return {"providers": providers}

    @app.get("/api/models")
    async def list_models(
        provider_id: str | None = None,
        available_only: bool = False,
        llm_only: bool = False,
    ):
        models = await app.state.db.list_provider_models(
            provider_id,
            available_only=available_only,
            llm_only=llm_only,
        )
        return {"models": [item.model_dump(mode="json") for item in models]}

    @app.post("/api/providers/{provider_id}/refresh")
    async def refresh_provider(provider_id: str):
        pid = provider_id.strip().lower()
        if pid not in SERVICE_PROVIDER_BY_ID:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_id}")
        disc = await _refresh_single_provider(pid)
        status_obj = await _build_provider_status(pid, discovery_override=disc)
        return status_obj.model_dump(mode="json")

    async def _terminate_login_session(session: dict[str, Any] | None) -> None:
        if not session:
            return
        proc = session.get("proc")
        if proc is None or getattr(proc, "returncode", None) is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass

    async def _read_available_output(proc: Any, timeout: float = 2.0) -> str:
        """Read available stdout/stderr incrementally without killing a waiting process."""
        stdout_stream = getattr(proc, "stdout", None)
        stderr_stream = getattr(proc, "stderr", None)
        has_async_streams = any(
            asyncio.iscoroutinefunction(getattr(s, "read", None))
            for s in (stdout_stream, stderr_stream)
            if s is not None
        )
        if not has_async_streams:
            if hasattr(proc, "communicate"):
                try:
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                    return (
                        (stdout or b"").decode(errors="replace")
                        + "\n"
                        + (stderr or b"").decode(errors="replace")
                    ).strip()
                except asyncio.TimeoutError:
                    return ""
            return ""

        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        tasks: dict[asyncio.Task[bytes], tuple[Any, list[str]]] = {}
        for stream, target in ((stdout_stream, stdout_chunks), (stderr_stream, stderr_chunks)):
            if stream is not None:
                tasks[asyncio.create_task(stream.read(4096))] = (stream, target)

        try:
            while tasks:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    break
                combined = "".join(stdout_chunks) + "\n" + "".join(stderr_chunks)
                url_seen = bool(re.search(r"https?://\S+", combined))
                wait_timeout = min(0.2, remaining) if url_seen else remaining
                done, _ = await asyncio.wait(
                    tasks.keys(),
                    timeout=wait_timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    if url_seen:
                        break
                    continue
                for task in done:
                    stream, target = tasks.pop(task)
                    try:
                        data = task.result()
                    except Exception:
                        continue
                    if data:
                        target.append(data.decode(errors="replace"))
                        tasks[asyncio.create_task(stream.read(4096))] = (stream, target)
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks.keys(), return_exceptions=True)

        stdout_text = "".join(stdout_chunks).strip()
        stderr_text = "".join(stderr_chunks).strip()
        return "\n".join(part for part in (stdout_text, stderr_text) if part)


    @app.post("/api/providers/{provider_id}/login")
    async def provider_login(provider_id: str):
        pid = provider_id.strip().lower()
        if pid in ("agy", "gemini"):
            if not shutil.which(pid):
                raise HTTPException(status_code=400, detail=f"{pid} binary not found in PATH")
            refresh_fn = getattr(app.state.generator, "refresh_provider", None)
            if callable(refresh_fn):
                disc = await refresh_fn(pid)
            else:
                disc = await discover_provider_models(pid)
                await app.state.db.replace_provider_models(pid, disc.models)
            status_obj = await _build_provider_status(
                pid,
                discovery_override=disc if isinstance(disc, DiscoveryResult) else None,
            )
            avail_models = [item.model_dump(mode="json") for item in status_obj.available_models]
            if status_obj.status == "authenticated":
                return {
                    "provider_id": pid,
                    "status": "authenticated",
                    "requires_callback": False,
                    "message": (
                        f"{status_obj.display_name} is authenticated "
                        f"({status_obj.available_model_count} available models)."
                    ),
                    "auth_url": None,
                    "device_url": None,
                    "device_code": None,
                    "available_models": avail_models,
                    "provider_status": status_obj.model_dump(mode="json"),
                }
            if pid == "agy":
                guidance = (
                    "Antigravity CLI (`agy`) does not provide a non-interactive login subcommand.\n"
                    "Sign in through your local Antigravity IDE / CLI session so `agy models` succeeds, "
                    'then click "Check Login Status" to verify authentication and discover available models.'
                )
            else:
                guidance = (
                    "Gemini CLI requires an interactive terminal for Google OAuth sign-in.\n"
                    "Run `gemini` once in an interactive terminal to sign in with your Google subscription "
                    "(stored in ~/.gemini/settings.json and ~/.gemini/oauth_creds.json), "
                    'then click "Check Login Status" to verify authentication and discover available models.'
                )
            return {
                "provider_id": pid,
                "status": "manual_action_required",
                "requires_callback": False,
                "message": guidance,
                "auth_url": None,
                "device_url": None,
                "device_code": None,
                "available_models": avail_models,
                "provider_status": status_obj.model_dump(mode="json"),
            }

        login_cmds = {
            "claude": ["claude", "auth", "login"],
            "codex": ["codex", "login"],
            "copilot": ["copilot", "login"],
        }
        cmd = login_cmds.get(pid)
        if cmd is None:
            raise HTTPException(status_code=400, detail=f"Login not supported for provider: {provider_id}")
        if not shutil.which(cmd[0]):
            return {
                "provider_id": pid,
                "status": "missing_binary",
                "requires_callback": False,
                "message": f"{cmd[0]} binary not found in PATH",
                "auth_url": None,
                "device_url": None,
                "device_code": None,
            }
        login_sessions: dict[str, dict[str, Any]] = getattr(app.state, "login_sessions", {})
        app.state.login_sessions = login_sessions
        await _terminate_login_session(login_sessions.pop(pid, None))
        try:
            env = os.environ.copy()
            for key in CLI_PROBE_STRIPPED_ENV:
                env.pop(key, None)
            env["BROWSER"] = "echo"
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            output = await _read_available_output(proc, timeout=2.0)
        except OSError as exc:
            return {
                "provider_id": pid,
                "status": "error",
                "requires_callback": False,
                "message": str(exc),
                "auth_url": None,
                "device_url": None,
                "device_code": None,
            }
        url_match = re.search(r"https?://\S+", output)
        code_match = re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{4}\b", output)
        auth_url = url_match.group(0).rstrip(".,)\"'<>]") if url_match else None
        device_code = code_match.group(0) if code_match else None
        if getattr(proc, "returncode", None) not in (None, 0) and auth_url is None and device_code is None:
            return {
                "provider_id": pid,
                "status": "error",
                "requires_callback": False,
                "message": output or f"{pid} login exited with code {proc.returncode}",
                "auth_url": None,
                "device_url": None,
                "device_code": None,
            }
        login_sessions[pid] = {
            "proc": proc,
            "auth_url": auth_url,
            "device_code": device_code,
            "output": output,
        }
        return {
            "provider_id": pid,
            "status": "awaiting_callback",
            "requires_callback": True,
            "message": output or f"Started {pid} login flow",
            "auth_url": auth_url,
            "device_url": auth_url,
            "device_code": device_code,
        }

    @app.post("/api/providers/{provider_id}/login/complete")
    async def provider_login_complete(provider_id: str, req: LoginCompleteRequest | None = None):
        pid = provider_id.strip().lower()
        meta = SERVICE_PROVIDER_BY_ID.get(pid)
        if meta is None or not meta["supports_login"]:
            raise HTTPException(status_code=400, detail=f"Login not supported for provider: {provider_id}")

        raw = (req.input if req is not None else "").strip()
        target_url: str | None = None
        if raw:
            lowered = raw.lower()
            if lowered.startswith("http://") or lowered.startswith("https://"):
                try:
                    parsed = urlsplit(raw)
                    port = parsed.port
                except ValueError:
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid callback URL: only http://localhost:<port> or http://127.0.0.1:<port> is allowed",
                    ) from None
                if (
                    parsed.scheme != "http"
                    or parsed.hostname not in {"localhost", "127.0.0.1"}
                    or parsed.username is not None
                    or parsed.password is not None
                    or port is None
                    or not (1 <= port <= 65535)
                ):
                    raise HTTPException(
                        status_code=400,
                        detail="Invalid callback URL: only http://localhost:<port> or http://127.0.0.1:<port> is allowed",
                    )
                target_url = f"http://127.0.0.1:{port}{parsed.path or ''}"
                if parsed.query:
                    target_url = f"{target_url}?{parsed.query}"

        login_sessions: dict[str, dict[str, Any]] = getattr(app.state, "login_sessions", {})
        session = login_sessions.get(pid)

        if session is not None and raw:
            proc = session["proc"]
            try:
                if target_url is not None:
                    try:
                        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                            await client.get(target_url)
                    except (httpx.HTTPError, OSError) as exc:
                        logger.warning("Localhost callback relay failed for %s: %s", pid, exc)
                else:
                    stdin = getattr(proc, "stdin", None)
                    if stdin is not None:
                        try:
                            stdin.write((raw + "\n").encode("utf-8"))
                            if hasattr(stdin, "drain"):
                                await stdin.drain()
                        except (BrokenPipeError, OSError) as exc:
                            logger.warning("Failed writing login code to %s stdin: %s", pid, exc)

                if getattr(proc, "returncode", None) is None and hasattr(proc, "wait"):
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        await _terminate_login_session(session)
            finally:
                await _terminate_login_session(session)
                login_sessions.pop(pid, None)

            await app.state.generator.refresh_catalog()
            status_obj = await _build_provider_status(pid)
            return status_obj.model_dump(mode="json")

        if session is not None:
            proc = session["proc"]
            if getattr(proc, "returncode", None) is not None:
                login_sessions.pop(pid, None)
            elif hasattr(proc, "wait"):
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.25)
                    login_sessions.pop(pid, None)
                except asyncio.TimeoutError:
                    pass

        disc = await _refresh_single_provider(pid)
        status_obj = await _build_provider_status(pid, discovery_override=disc)
        return status_obj.model_dump(mode="json")

    @app.post("/api/providers/credentials")
    async def save_credentials(req: CredentialsRequest):
        pid = req.provider.strip().lower()
        try:
            save_provider_credentials(pid, req.api_key, base_url=req.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        disc = await _refresh_single_provider(pid)
        caps = await app.state.db.list_capabilities()
        status_obj = await _build_provider_status(pid, capabilities=caps, discovery_override=disc)
        available_models = [item.model_dump(mode="json") for item in status_obj.available_models]
        return {
            "status": "saved",
            "provider": pid,
            "catalog_count": len(caps),
            "available_models": available_models,
            "available_model_count": len(available_models),
            "provider_status": status_obj.model_dump(mode="json"),
        }

    @app.get("/api/usage/summary")
    async def usage_summary():
        return await app.state.db.get_usage_summary()

    @app.get("/api/rolodex")
    async def rolodex():
        entries = await app.state.generator.get_rolodex()
        text = app.state.generator.generate_rolodex_text(entries)
        return {"entries": entries, "text": text}

    @app.get("/api/skills")
    async def list_skills():
        from .skills import discover_skills

        workspace_root = getattr(app.state, "workspace_root", Path.cwd())
        skills = discover_skills(workspace_root, getattr(app.state, "skill_roots", ()))
        counts_by_source: dict[str, int] = {}
        for skill in skills:
            counts_by_source[skill.source] = counts_by_source.get(skill.source, 0) + 1
        return {
            "skills": [s.to_summary().model_dump() for s in skills],
            "count": len(skills),
            "sources": counts_by_source,
        }

    @app.post("/api/containers/{container_id}/downshift")
    async def downshift_container(container_id: str, req: DownshiftRequest | None = None):
        body = req or DownshiftRequest()
        try:
            container = await app.state.generator.downshift_container(
                container_id,
                target_model=body.target_model,
                target_container_id=body.target_container_id,
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = 404 if detail.lower().startswith("unknown container") else 400
            raise HTTPException(status_code=status_code, detail=detail) from None
        return {"status": "downshifted", "container": container}

    @app.get("/api/tasks")
    async def tasks():
        return await app.state.db.list_tasks()

    @app.get("/api/tasks/{task_id}/logs")
    async def task_logs(task_id: str, run_id: str | None = None, limit: int = 500):
        if not (1 <= limit <= 2000):
            raise HTTPException(status_code=422, detail="limit must be between 1 and 2000")
        task = await app.state.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        effective_run_id = run_id if run_id is not None else task.run_id
        logs = await app.state.db.list_card_logs(task_id, run_id=run_id, limit=limit)
        return {
            "task_id": task_id,
            "run_id": effective_run_id,
            "logs": logs,
        }

    @app.post("/api/tasks", status_code=201)
    async def create_task(payload: dict[str, Any]):
        data = dict(payload)
        auto_start = data.pop("auto_start", False)
        if not isinstance(auto_start, bool):
            raise HTTPException(status_code=422, detail="auto_start must be a boolean")
        try:
            task = KanbanTask.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if task.status == "Done":
            raise HTTPException(status_code=409, detail="Tasks must be approved before completion")
        if task.run_id is not None:
            raise HTTPException(status_code=422, detail="Run id is assigned by the server")
        if await app.state.db.get_task(task.task_id):
            raise HTTPException(status_code=409, detail="Task already exists")
        if auto_start or task.status == "Running":
            task.run_id = str(uuid4())
            task.status = "Running"
        await app.state.db.upsert_task(task)
        await app.state.bus.publish({"type": "task_updated", "task_id": task.task_id})
        if task.status == "Running":
            launch(
                app.state.orchestrator.start(
                    task.run_id, task.description or task.title, kanban_id=task.task_id
                ),
                task.run_id,
            )
        return task

    @app.patch("/api/tasks/{task_id}")
    async def update_task(task_id: str, changes: dict[str, Any]):
        task = await app.state.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if "task_id" in changes and changes["task_id"] != task_id:
            raise HTTPException(status_code=422, detail="Task id cannot be changed")
        if "run_id" in changes and changes["run_id"] != task.run_id:
            raise HTTPException(status_code=422, detail="Run id is assigned by the server")
        if changes.get("status") == "Done" and task.status != "Done":
            raise HTTPException(status_code=409, detail="Tasks must be approved before completion")
        previous_status = task.status
        try:
            task = KanbanTask.model_validate(
                {**task.model_dump(), **changes, "task_id": task_id,
                 "updated_at": datetime.now(timezone.utc)}
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        start = previous_status != "Running" and task.status == "Running"
        if start:
            if task.run_id in app.state.active_runs:
                raise HTTPException(status_code=409, detail="Previous run is still executing")
            task.run_id = str(uuid4())
        await app.state.db.upsert_task(task)
        await app.state.bus.publish({"type": "task_updated", "task_id": task_id})
        if start:
            launch(
                app.state.orchestrator.start(
                    task.run_id, task.description or task.title, kanban_id=task.task_id
                ),
                task.run_id,
            )
        return task

    @app.post("/api/run", status_code=202)
    async def run(request: RunRequest):
        if not request.goal.strip():
            raise HTTPException(status_code=422, detail="Goal must not be empty")
        run_id = request.run_id or str(uuid4())
        if run_id in app.state.active_runs or any(
            item.run_id == run_id for item in await app.state.db.list_tasks()
        ):
            raise HTTPException(status_code=409, detail="Run id is already in use")
        task = KanbanTask(title=request.goal, description=request.goal, status="Running", run_id=run_id)
        await app.state.db.upsert_task(task)
        launch(app.state.orchestrator.start(run_id, request.goal, kanban_id=task.task_id), run_id)
        return {"run_id": run_id}

    @app.get("/api/runs/{run_id}")
    async def run_state(run_id: str):
        return await app.state.orchestrator.get_state(run_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str):
        return await app.state.db.list_events(run_id=run_id)

    @app.post("/api/runs/{run_id}/review", status_code=202)
    async def review(run_id: str, request: ReviewRequest):
        launch(app.state.orchestrator.resume(run_id, request.decision), run_id)
        return {"run_id": run_id}

    @app.post("/api/tasks/{task_id}/approve", status_code=202)
    async def approve_task(task_id: str):
        task = await app.state.db.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")
        if not task.run_id:
            raise HTTPException(status_code=409, detail="Task has no run to approve")
        state = await app.state.orchestrator.get_state(task.run_id)
        if "human_review_gate" not in state.get("next", []):
            raise HTTPException(status_code=409, detail="Run is not awaiting review")
        launch(app.state.orchestrator.resume(task.run_id, True), task.run_id)
        return {"run_id": task.run_id}

    @app.websocket("/ws")
    async def websocket_graph(websocket: WebSocket):
        if not _trusted_websocket_origin(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = await app.state.bus.subscribe()
        try:
            await websocket.send_json(await app.state.orchestrator.get_graph())
            while True:
                receive = asyncio.create_task(websocket.receive())
                update = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait(
                        (receive, update), return_when=asyncio.FIRST_COMPLETED
                    )
                    if receive in done and receive.result()["type"] == "websocket.disconnect":
                        break
                    if update in done:
                        await websocket.send_json(await app.state.orchestrator.get_graph())
                finally:
                    for pending in (receive, update):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(receive, update, return_exceptions=True)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                await app.state.bus.unsubscribe(queue)

    @app.websocket("/ws/graph")
    async def websocket_events(websocket: WebSocket):
        if not _trusted_websocket_origin(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        queue = await app.state.bus.subscribe()
        try:
            while True:
                receive = asyncio.create_task(websocket.receive())
                update = asyncio.create_task(queue.get())
                try:
                    done, _ = await asyncio.wait(
                        (receive, update), return_when=asyncio.FIRST_COMPLETED
                    )
                    if receive in done and receive.result()["type"] == "websocket.disconnect":
                        break
                    if update in done:
                        event = update.result()
                        if "event_type" in event and "run_id" in event:
                            await websocket.send_json(event)
                finally:
                    for pending in (receive, update):
                        if not pending.done():
                            pending.cancel()
                    await asyncio.gather(receive, update, return_exceptions=True)
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            with anyio.CancelScope(shield=True):
                await app.state.bus.unsubscribe(queue)

    @app.get("/api/project")
    async def project_info():
        return {
            "project": str(getattr(app.state, "workspace_root", project_root)),
            "state_dir": str(getattr(app.state, "data_path", data_path)),
        }

    # Serve the built UI from the same origin, so `dagger` needs no separate frontend server.
    # Mounted last: the API and WebSocket routes above take precedence.
    dist = paths.frontend_dist_dir()
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    return app


app = create_app()
