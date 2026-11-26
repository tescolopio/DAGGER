"""Async SQLite storage for the control plane."""

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from .schema import AgentContainer, EndpointCapability, GraphEvent, KanbanTask, UsageEvent

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
                    CREATE TABLE IF NOT EXISTS usage_events (
                        event_id TEXT PRIMARY KEY NOT NULL,
                        timestamp TEXT NOT NULL,
                        run_id TEXT,
                        kanban_id TEXT,
                        container_id TEXT,
                        provider TEXT NOT NULL,
                        model TEXT,
                        cost_tier TEXT NOT NULL,
                        duration_ms INTEGER NOT NULL,
                        input_tokens_est INTEGER NOT NULL,
                        output_tokens_est INTEGER NOT NULL,
                        estimated_cost_usd REAL NOT NULL,
                        success INTEGER NOT NULL,
                        data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS usage_events_ts_idx
                        ON usage_events(timestamp);
                    CREATE INDEX IF NOT EXISTS usage_events_provider_idx
                        ON usage_events(provider, timestamp);
                    CREATE TABLE IF NOT EXISTS card_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        log_id TEXT UNIQUE NOT NULL,
                        kanban_id TEXT NOT NULL,
                        run_id TEXT NOT NULL,
                        milestone_id TEXT,
                        node_id TEXT NOT NULL,
                        provider TEXT NOT NULL,
                        channel TEXT NOT NULL,
                        text TEXT NOT NULL,
                        ts TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS card_logs_kanban_idx
                        ON card_logs(kanban_id, id);
                    CREATE INDEX IF NOT EXISTS card_logs_run_idx
                        ON card_logs(run_id, id);
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

    async def record_usage_event(self, event: UsageEvent) -> UsageEvent:
        event = UsageEvent.model_validate(
            event.model_dump() if isinstance(event, UsageEvent) else event
        )
        await self._write(
            """INSERT OR IGNORE INTO usage_events (
                   event_id, timestamp, run_id, kanban_id, container_id,
                   provider, model, cost_tier, duration_ms,
                   input_tokens_est, output_tokens_est, estimated_cost_usd,
                   success, data
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                event.event_id,
                event.timestamp.isoformat(),
                event.run_id,
                event.kanban_id,
                event.container_id,
                event.provider,
                event.model,
                event.cost_tier,
                event.duration_ms,
                event.input_tokens_est,
                event.output_tokens_est,
                event.estimated_cost_usd,
                1 if event.success else 0,
                event.model_dump_json(),
            ),
        )
        return event

    async def list_usage_events(
        self, since_iso: str | None = None, limit: int = 500
    ) -> list[UsageEvent]:
        clauses, values = [], []
        if since_iso is not None:
            clauses.append("timestamp >= ?")
            values.append(since_iso)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, limit))
        rows = await self._read(
            "SELECT data FROM usage_events" + where + " ORDER BY timestamp DESC, event_id DESC LIMIT ?",
            tuple(values),
        )
        return [UsageEvent.model_validate_json(row[0]) for row in rows]

    async def get_usage_summary(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        cutoff_3h = (now - timedelta(hours=3)).isoformat()
        cutoff_24h = (now - timedelta(hours=24)).isoformat()
        cutoff_7d = (now - timedelta(days=7)).isoformat()

        total_rows = await self._read("SELECT COUNT(*) FROM usage_events")
        total_events = int(total_rows[0][0]) if total_rows else 0

        sub_limits = {
            "claude": (45, 200),
            "codex": (45, 200),
            "gemini": (60, 300),
            "copilot": (50, 250),
            "agy": (60, 300),
        }
        subscriptions: list[dict[str, Any]] = []
        for provider, (limit_3h, limit_24h) in sub_limits.items():
            rows_3h = await self._read(
                "SELECT COUNT(*) FROM usage_events WHERE provider=? AND timestamp >= ?",
                (provider, cutoff_3h),
            )
            rows_24h = await self._read(
                "SELECT COUNT(*) FROM usage_events WHERE provider=? AND timestamp >= ?",
                (provider, cutoff_24h),
            )
            calls_3h = int(rows_3h[0][0]) if rows_3h else 0
            calls_24h = int(rows_24h[0][0]) if rows_24h else 0
            subscriptions.append(
                {
                    "provider": provider,
                    "calls_3h": calls_3h,
                    "calls_24h": calls_24h,
                    "soft_limit_3h": limit_3h,
                    "soft_limit_24h": limit_24h,
                    "utilization_3h": round(min(1.0, calls_3h / limit_3h), 4) if limit_3h > 0 else 0.0,
                }
            )

        metered_totals = await self._read(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0.0), COUNT(*) FROM usage_events WHERE cost_tier='metered_api'"
        )
        total_cost_usd = round(float(metered_totals[0][0]), 6) if metered_totals else 0.0
        total_metered_calls = int(metered_totals[0][1]) if metered_totals else 0

        metered_7d = await self._read(
            "SELECT COALESCE(SUM(estimated_cost_usd), 0.0) FROM usage_events WHERE cost_tier='metered_api' AND timestamp >= ?",
            (cutoff_7d,),
        )
        cost_7d_usd = round(float(metered_7d[0][0]), 6) if metered_7d else 0.0

        trend_rows = await self._read(
            """SELECT substr(timestamp, 1, 10) AS day,
                      COALESCE(SUM(estimated_cost_usd), 0.0),
                      COUNT(*)
               FROM usage_events
               WHERE cost_tier='metered_api' AND timestamp >= ?
               GROUP BY day
               ORDER BY day ASC""",
            (cutoff_7d,),
        )
        daily_trend = [
            {"date": row[0], "cost_usd": round(float(row[1]), 6), "calls": int(row[2])}
            for row in trend_rows
        ]

        containers = await self.list_containers()
        active_containers = [c for c in containers if c.active]
        cheaper_tiers = {"local_free": 0, "flat_subscription": 1, "metered_api": 2}
        downshift_suggestions: list[dict[str, Any]] = []
        for current in active_containers:
            if current.cost_tier not in ("metered_api", "flat_subscription"):
                continue
            current_strengths = set(current.strengths)
            for candidate in active_containers:
                if candidate.container_id == current.container_id:
                    continue
                if cheaper_tiers.get(candidate.cost_tier, 2) >= cheaper_tiers.get(current.cost_tier, 2):
                    continue
                if candidate.role == current.role or (
                    current_strengths and current_strengths.intersection(candidate.strengths)
                ):
                    downshift_suggestions.append(
                        {
                            "container_id": current.container_id,
                            "current_model": current.model or current.provider,
                            "suggested_container_id": candidate.container_id,
                            "suggested_model": candidate.model or candidate.provider,
                            "reason": f"Active {candidate.cost_tier} container matches role/strengths",
                            "estimated_savings_pct": 100 if candidate.cost_tier == "local_free" else 80,
                        }
                    )
                    break

        return {
            "subscriptions": subscriptions,
            "metered": {
                "total_cost_usd": total_cost_usd,
                "cost_7d_usd": cost_7d_usd,
                "total_calls": total_metered_calls,
                "daily_trend": daily_trend,
            },
            "downshift_suggestions": downshift_suggestions,
            "total_events": total_events,
        }

    async def append_card_log(
        self, entry: dict[str, Any], *, max_per_run: int = 2000
    ) -> dict[str, Any]:
        max_text = 4000
        raw_text = str(entry.get("text", ""))
        if len(raw_text) > max_text:
            raw_text = raw_text[: max_text - 3] + "..."
        row = {
            "event_type": "CARD_LOG",
            "log_id": str(entry.get("log_id") or ""),
            "kanban_id": str(entry.get("kanban_id") or ""),
            "run_id": str(entry.get("run_id") or ""),
            "milestone_id": entry.get("milestone_id"),
            "node_id": str(entry.get("node_id") or ""),
            "provider": str(entry.get("provider") or "system"),
            "channel": str(entry.get("channel") or "system"),
            "text": raw_text,
            "ts": str(entry.get("ts") or ""),
        }
        async with self._lock:
            assert self._connection is not None
            conn = self._connection
            await conn.execute(
                """
                INSERT OR IGNORE INTO card_logs
                    (log_id, kanban_id, run_id, milestone_id, node_id, provider, channel, text, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["log_id"],
                    row["kanban_id"],
                    row["run_id"],
                    row["milestone_id"],
                    row["node_id"],
                    row["provider"],
                    row["channel"],
                    row["text"],
                    row["ts"],
                ),
            )
            if max_per_run > 0:
                if row["run_id"]:
                    await conn.execute(
                        """
                        DELETE FROM card_logs
                        WHERE run_id = ? AND id NOT IN (
                            SELECT id FROM card_logs WHERE run_id = ? ORDER BY id DESC LIMIT ?
                        )
                        """,
                        (row["run_id"], row["run_id"], max_per_run),
                    )
                else:
                    await conn.execute(
                        """
                        DELETE FROM card_logs
                        WHERE kanban_id = ? AND run_id = '' AND id NOT IN (
                            SELECT id FROM card_logs WHERE kanban_id = ? AND run_id = '' ORDER BY id DESC LIMIT ?
                        )
                        """,
                        (row["kanban_id"], row["kanban_id"], max_per_run),
                    )
            await conn.commit()
        return row

    async def list_card_logs(
        self, kanban_id: str, *, run_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(int(limit), 2000))
        async with self._lock:
            assert self._connection is not None
            conn = self._connection
            if run_id is not None:
                cursor = await conn.execute(
                    """
                    SELECT log_id, kanban_id, run_id, milestone_id, node_id, provider, channel, text, ts
                    FROM card_logs
                    WHERE kanban_id = ? AND run_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (kanban_id, run_id, bounded_limit),
                )
            else:
                cursor = await conn.execute(
                    """
                    SELECT log_id, kanban_id, run_id, milestone_id, node_id, provider, channel, text, ts
                    FROM card_logs
                    WHERE kanban_id = ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (kanban_id, bounded_limit),
                )
            rows = await cursor.fetchall()
        rows.reverse()
        return [
            {
                "event_type": "CARD_LOG",
                "log_id": r[0],
                "kanban_id": r[1],
                "run_id": r[2],
                "milestone_id": r[3],
                "node_id": r[4],
                "provider": r[5],
                "channel": r[6],
                "text": r[7],
                "ts": r[8],
            }
            for r in rows
        ]
