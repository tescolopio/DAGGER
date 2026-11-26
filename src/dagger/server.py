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
    DefinitionOfDone,
    KanbanTask,
    ProviderCredentialStatus,
    TaskEnvelope,
)
from dagger.providers import ProviderRegistry
from dagger.vault import get_vault_credential, save_provider_credentials

logger = logging.getLogger(__name__)


class LoginCompleteRequest(BaseModel):
    input: str


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
    api_key: str
    base_url: str | None = None


class DownshiftRequest(BaseModel):
    target_model: str | None = None
    target_container_id: str | None = None


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
                    healthy = {
                        (item.provider, item.model_name)
                        for item in capabilities if item.healthy
                    }
                    container = next(
                        (item for item in containers if item.active and
                         any(provider == item.provider and
                             (item.model is None or model == item.model)
                             for provider, model in healthy)),
                        None,
                    )
                    if container is None:
                        raise RuntimeError("No available agent container is configured")
                    manager.provider = container.provider
                    manager.model = container.model
                    result = await registry.get(container.provider).run(
                        container, TaskEnvelope(
                            task_id=str(uuid4()), run_id=str(uuid4()), kanban_id=str(uuid4()),
                            prompt=prompt, container_id=container.container_id,
                            dod=DefinitionOfDone(acceptance_criteria=[]),
                        )
                    )
                    return result if isinstance(result, str) else str(result)

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

    @app.get("/api/providers/status")
    async def providers_status():
        capabilities = await app.state.db.list_capabilities()
        providers: list[dict[str, Any]] = []
        cli_specs = [
            ("claude", "Claude Code CLI", "claude"),
            ("codex", "Codex CLI", "codex"),
            ("gemini", "Gemini CLI", "gemini"),
            ("copilot", "GitHub Copilot CLI", "copilot"),
            ("agy", "Antigravity CLI", "agy"),
        ]
        for pid, display, binary in cli_specs:
            found = shutil.which(binary)
            prov_caps = [item for item in capabilities if item.provider == pid]
            if not found:
                status = "missing_binary"
                detail = f"{binary} binary not found in PATH"
            elif pid == "gemini":
                from dagger.agents.generator import _has_gemini_auth

                if not _has_gemini_auth():
                    status = "expired"
                    detail = "gemini CLI found, but no active selectedType/oauth_creds.json or API/Vertex/GCA env"
                elif prov_caps and not any(item.healthy for item in prov_caps):
                    status = "expired"
                    detail = f"{binary} found at {found}, but health check failed"
                else:
                    status = "authenticated"
                    detail = f"Found at {found}"
            elif prov_caps and not any(item.healthy for item in prov_caps):
                status = "expired"
                detail = f"{binary} found at {found}, but health check failed"
            else:
                status = "authenticated"
                detail = f"Found at {found}"
            providers.append(
                ProviderCredentialStatus(
                    provider_id=pid,
                    display_name=display,
                    kind="subscription_cli",
                    status=status,
                    detail=detail,
                    supports_login=True,
                    supports_api_key=False,
                ).model_dump()
            )
        for pid, display, env_key in (
            ("groq", "Groq API", "GROQ_API_KEY"),
            ("tavily", "Tavily Search API", "TAVILY_API_KEY"),
        ):
            configured = bool(get_vault_credential(env_key))
            providers.append(
                ProviderCredentialStatus(
                    provider_id=pid,
                    display_name=display,
                    kind="api_key",
                    status="api_key_configured" if configured else "unconfigured",
                    detail="Configured in local vault/env" if configured else "No API key configured",
                    supports_login=False,
                    supports_api_key=True,
                ).model_dump()
            )
        local_healthy = any(
            item.cost_tier == "local_free" and item.healthy for item in capabilities
        )
        providers.append(
            ProviderCredentialStatus(
                provider_id="local",
                display_name="Local OpenAI-Compatible",
                kind="local",
                status="authenticated" if local_healthy else "unconfigured",
                detail="Healthy local endpoint detected" if local_healthy else "No local endpoint active",
                supports_login=False,
                supports_api_key=False,
            ).model_dump()
        )
        return {"providers": providers}

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
        login_cmds = {
            "claude": ["claude", "auth", "login"],
            "codex": ["codex", "login"],
            "gemini": ["gemini"],
            "copilot": ["copilot", "auth", "login"],
            "agy": ["agy", "auth", "login"],
        }
        pid = provider_id.lower()
        cmd = login_cmds.get(pid)
        if cmd is None:
            raise HTTPException(status_code=400, detail=f"Login not supported for provider: {provider_id}")
        if not shutil.which(cmd[0]):
            return {
                "provider_id": pid,
                "status": "missing_binary",
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
            for key in (
                "GROQ_API_KEY",
                "TAVILY_API_KEY",
                "DAGGER_API_KEY",
                "DAGGER_API_BASE_URL",
                "OPENAI_API_KEY",
                "ANTHROPIC_API_KEY",
                "GEMINI_API_KEY",
                "GOOGLE_API_KEY",
                "GITHUB_TOKEN",
                "GH_TOKEN",
            ):
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
            "message": output or f"Started {pid} login flow",
            "auth_url": auth_url,
            "device_url": auth_url,
            "device_code": device_code,
        }

    @app.post("/api/providers/{provider_id}/login/complete")
    async def provider_login_complete(provider_id: str, req: LoginCompleteRequest):
        pid = provider_id.lower()
        login_sessions: dict[str, dict[str, Any]] = getattr(app.state, "login_sessions", {})
        session = login_sessions.get(pid)
        if session is None:
            raise HTTPException(status_code=404, detail=f"No active login session for provider: {provider_id}")
        raw = req.input.strip()
        if not raw:
            raise HTTPException(status_code=400, detail="Input must not be empty")

        proc = session["proc"]
        lowered = raw.lower()
        target_url: str | None = None
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
        status_payload = await providers_status()
        for item in status_payload.get("providers", []):
            if item.get("provider_id") == pid:
                return item
        raise HTTPException(status_code=404, detail=f"Provider status not found for: {provider_id}")

    @app.post("/api/providers/credentials")
    async def save_credentials(req: CredentialsRequest):
        try:
            save_provider_credentials(req.provider, req.api_key, base_url=req.base_url)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        await app.state.generator.refresh_catalog()
        caps = await app.state.db.list_capabilities()
        return {"status": "saved", "provider": req.provider.lower(), "catalog_count": len(caps)}

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
            raise HTTPException(status_code=404, detail=str(exc)) from None
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
