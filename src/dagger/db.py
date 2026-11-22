"""Async SQLite storage for the control plane."""

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from .schema import AgentContainer, EndpointCapability, GraphEvent, KanbanTask

_DEFAULT_SEED = Path(__file__).resolve().parents[2] / "containers" / "seed_containers.yaml"


class DatabaseManager:
    def __init__(
        self, path: str | Path = "dagger.db", *, seed_path: str | Path | bool | None = None
    ) -> None:
        self.path = str(path)
        self.seed_path = seed_path
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "DatabaseManager":
        await self.initialize()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def initialize(self) -> None:
        async with self._lock:
            if self._connection is not None:
                return
            if self.path != ":memory:" and not self.path.startswith("file:"):
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            connection = await aiosqlite.connect(self.path, uri=self.path.startswith("file:"))
            try:
                await connection.execute("PRAGMA journal_mode=WAL")
                await connection.execute("PRAGMA foreign_keys=ON")
                await connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS agent_containers (
                        container_id TEXT PRIMARY KEY NOT NULL,
                        data TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS endpoint_capabilities (
                        provider TEXT NOT NULL,
                        model_name TEXT NOT NULL,
                        data TEXT NOT NULL,
                        PRIMARY KEY(provider, model_name)
                    );
                    CREATE TABLE IF NOT EXISTS kanban_tasks (
                        task_id TEXT PRIMARY KEY NOT NULL,
                        run_id TEXT,
                        data TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS graph_events (
                        event_id TEXT PRIMARY KEY NOT NULL,
                        run_id TEXT NOT NULL,
                        kanban_id TEXT NOT NULL REFERENCES kanban_tasks(task_id),
                        timestamp TEXT NOT NULL,
                        data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS graph_events_run_idx
                        ON graph_events(run_id, timestamp);
                    CREATE INDEX IF NOT EXISTS graph_events_kanban_idx
                        ON graph_events(kanban_id, timestamp);
                    """
                )
                if self.seed_path is not False:
                    seed_path = Path(self.seed_path) if self.seed_path is not None else _DEFAULT_SEED
                    if seed_path.exists():
                        await self._load_seed(connection, seed_path)
                    elif self.seed_path is not None:
                        raise FileNotFoundError(seed_path)
                await connection.commit()
            except BaseException:
                await connection.close()
                raise
            self._connection = connection

    @staticmethod
    async def _load_seed(connection: aiosqlite.Connection, path: Path) -> None:
        with path.open(encoding="utf-8") as file:
            raw: Any = yaml.safe_load(file)
        if isinstance(raw, dict):
            raw = raw.get("containers")
        if not isinstance(raw, list):
            raise ValueError("Seed file must contain a list of containers")
        if any(not isinstance(item, dict) or not item.get("container_id") for item in raw):
            raise ValueError("Each seed container must have a stable container_id")
        containers = [AgentContainer.model_validate(item) for item in raw]
        for container in containers:
            await connection.execute(
                "INSERT OR IGNORE INTO agent_containers (container_id, data) VALUES (?, ?)",
                (container.container_id, container.model_dump_json()),
            )

    async def close(self) -> None:
        async with self._lock:
            if self._connection is not None:
                await self._connection.close()
                self._connection = None

    async def _get_connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            await self.initialize()
        assert self._connection is not None
        return self._connection

    async def _write(self, sql: str, values: tuple[Any, ...]) -> int:
        connection = await self._get_connection()
        async with self._lock:
            try:
                async with connection.execute(sql, values) as cursor:
                    count = cursor.rowcount
                await connection.commit()
                return count
            except BaseException:
                await connection.rollback()
                raise

    async def _read(self, sql: str, values: tuple[Any, ...] = ()) -> list[tuple]:
        connection = await self._get_connection()
        async with self._lock:
            async with connection.execute(sql, values) as cursor:
                return await cursor.fetchall()

    async def upsert_container(self, container: AgentContainer) -> AgentContainer:
        container = AgentContainer.model_validate(
            container.model_dump() if isinstance(container, AgentContainer) else container
        )
        await self._write(
            """INSERT INTO agent_containers (container_id, data) VALUES (?, ?)
               ON CONFLICT(container_id) DO UPDATE SET data=excluded.data""",
            (container.container_id, container.model_dump_json()),
        )
        return container

    async def get_container(self, container_id: str) -> AgentContainer | None:
        rows = await self._read("SELECT data FROM agent_containers WHERE container_id=?", (container_id,))
        return AgentContainer.model_validate_json(rows[0][0]) if rows else None

    async def list_containers(self) -> list[AgentContainer]:
        rows = await self._read("SELECT data FROM agent_containers ORDER BY container_id")
        return [AgentContainer.model_validate_json(row[0]) for row in rows]

    async def delete_container(self, container_id: str) -> bool:
        return bool(await self._write("DELETE FROM agent_containers WHERE container_id=?", (container_id,)))

    async def upsert_capability(self, capability: EndpointCapability) -> EndpointCapability:
        capability = EndpointCapability.model_validate(
            capability.model_dump() if isinstance(capability, EndpointCapability) else capability
        )
        await self._write(
            """INSERT INTO endpoint_capabilities (provider, model_name, data) VALUES (?, ?, ?)
               ON CONFLICT(provider, model_name) DO UPDATE SET data=excluded.data""",
            (capability.provider, capability.model_name, capability.model_dump_json()),
        )
        return capability

    async def get_capability(self, provider: str, model_name: str) -> EndpointCapability | None:
        rows = await self._read(
            "SELECT data FROM endpoint_capabilities WHERE provider=? AND model_name=?",
            (provider, model_name),
        )
        return EndpointCapability.model_validate_json(rows[0][0]) if rows else None

    async def list_capabilities(self) -> list[EndpointCapability]:
        rows = await self._read("SELECT data FROM endpoint_capabilities ORDER BY provider, model_name")
        return [EndpointCapability.model_validate_json(row[0]) for row in rows]

    async def delete_capability(self, provider: str, model_name: str) -> bool:
        return bool(await self._write(
            "DELETE FROM endpoint_capabilities WHERE provider=? AND model_name=?", (provider, model_name)
        ))

    async def upsert_task(self, task: KanbanTask) -> KanbanTask:
        task = KanbanTask.model_validate(
            task.model_dump() if isinstance(task, KanbanTask) else task
        )
        await self._write(
            """INSERT INTO kanban_tasks (task_id, run_id, data) VALUES (?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET run_id=excluded.run_id, data=excluded.data""",
            (task.task_id, task.run_id, task.model_dump_json()),
        )
        return task

    async def get_task(self, task_id: str) -> KanbanTask | None:
        rows = await self._read("SELECT data FROM kanban_tasks WHERE task_id=?", (task_id,))
        return KanbanTask.model_validate_json(rows[0][0]) if rows else None

    async def list_tasks(self) -> list[KanbanTask]:
        rows = await self._read("SELECT data FROM kanban_tasks ORDER BY task_id")
        return [KanbanTask.model_validate_json(row[0]) for row in rows]

    async def delete_task(self, task_id: str) -> bool:
        return bool(await self._write("DELETE FROM kanban_tasks WHERE task_id=?", (task_id,)))

    async def upsert_event(self, event: GraphEvent) -> GraphEvent:
        event = GraphEvent.model_validate(
            event.model_dump() if isinstance(event, GraphEvent) else event
        )
        await self._write(
            """INSERT INTO graph_events (event_id, run_id, kanban_id, timestamp, data)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(event_id) DO UPDATE SET
               run_id=excluded.run_id, kanban_id=excluded.kanban_id,
               timestamp=excluded.timestamp, data=excluded.data""",
            (event.event_id, event.run_id, event.kanban_id, event.timestamp.isoformat(), event.model_dump_json()),
        )
        return event

    async def get_event(self, event_id: str) -> GraphEvent | None:
        rows = await self._read("SELECT data FROM graph_events WHERE event_id=?", (event_id,))
        return GraphEvent.model_validate_json(rows[0][0]) if rows else None

    async def list_events(
        self, run_id: str | None = None, kanban_id: str | None = None
    ) -> list[GraphEvent]:
        clauses, values = [], []
        if run_id is not None:
            clauses.append("run_id=?")
            values.append(run_id)
        if kanban_id is not None:
            clauses.append("kanban_id=?")
            values.append(kanban_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = await self._read(
            "SELECT data FROM graph_events" + where + " ORDER BY timestamp, event_id", tuple(values)
        )
        return [GraphEvent.model_validate_json(row[0]) for row in rows]

    async def delete_event(self, event_id: str) -> bool:
        return bool(await self._write("DELETE FROM graph_events WHERE event_id=?", (event_id,)))
