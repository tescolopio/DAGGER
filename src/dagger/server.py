"""Local control-plane API and graph event stream."""

from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import anyio
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel, ValidationError
from watchfiles import awatch

from dagger.agents.generator import AgentGenerator
from dagger.agents.orchestrator import Orchestrator
from dagger.agents.project_manager import ProjectManager
from dagger.bus import EventBus
from dagger.db import DatabaseManager
from dagger.schema import AgentContainer, DefinitionOfDone, KanbanTask, TaskEnvelope
from dagger.providers import ProviderRegistry

logger = logging.getLogger(__name__)


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


def create_app(
    db_path: str | Path | None = None,
    checkpoint_path: str | Path | None = None,
    *,
    generator_factory: Any = AgentGenerator,
    container_dir: str | Path = "containers",
) -> FastAPI:
    """Create an app, optionally overriding storage paths for an isolated instance."""
    data_path = Path(os.environ.get("DAGGER_DATA_DIR", "data"))
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
                    result = await registry.get(container.provider).run(
                        container, TaskEnvelope(
                            task_id=str(uuid4()), run_id=str(uuid4()), kanban_id=str(uuid4()),
                            prompt=prompt, container_id=container.container_id,
                            dod=DefinitionOfDone(acceptance_criteria=[]),
                        )
                    )
                    return result if isinstance(result, str) else str(result)

                manager = ProjectManager(planner=execute, worker=execute)
                bus_path = data_path / "bus"
                orchestrator = Orchestrator(
                    db, registry, manager, bus=bus, checkpointer=checkpointer,
                    bus_path=bus_path,
                )
                bus_path.mkdir(parents=True, exist_ok=True)
                definitions_path = Path(container_dir)
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

                for path in definitions_path.iterdir():
                    await load_definition(path, initial=True)

                generator = generator_factory(
                    db, job_store_path=data_path / "scheduler.sqlite3",
                    export_dir=definitions_path,
                )
                await generator.refresh_catalog()
                generator.start()
                app.state.db = db
                app.state.bus = bus
                app.state.orchestrator = orchestrator
                app.state.generator = generator
                app.state.jobs = jobs
                app.state.active_runs = set()

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

    @app.get("/api/tasks")
    async def tasks():
        return await app.state.db.list_tasks()

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

    return app


app = create_app()
