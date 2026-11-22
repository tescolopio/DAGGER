import sqlite3

import pytest
from pydantic import ValidationError

from dagger.db import DatabaseManager
from dagger.schema import AgentContainer, EndpointCapability, GraphEvent, KanbanTask


def test_strict_validation():
    with pytest.raises(ValidationError):
        AgentContainer(name="example", role="coder", provider="other")
    with pytest.raises(ValidationError):
        AgentContainer(name="example", role="coder", provider="claude", max_concurrency="2")
    with pytest.raises(ValidationError):
        KanbanTask(title="example", status="started")
    with pytest.raises(ValidationError):
        KanbanTask(title="example", extra="unexpected")
    with pytest.raises(ValidationError):
        GraphEvent(run_id="r", kanban_id="k", target_node="worker", provider="system",
                   event_type="unexpected")


async def test_crud_persistence_foreign_keys_and_wal(db_path):
    async with DatabaseManager(db_path, seed_path=False) as db:
        container = AgentContainer(container_id="agent", name="Agent", role="coder", provider="claude")
        capability = EndpointCapability(provider="claude", model_name="model")
        task = KanbanTask(task_id="task", title="Task")
        event = GraphEvent(run_id="run", kanban_id="task", event_type="NODE_SPAWNED",
                           target_node="agent", provider="system")
        with pytest.raises(sqlite3.IntegrityError):
            await db.upsert_event(event)
        await db.upsert_container(container)
        await db.upsert_capability(capability)
        await db.upsert_task(task)
        await db.upsert_event(event)
        assert await db.list_events(run_id="run") == [event]
        assert await db.list_events(run_id="another") == []
        with pytest.raises(sqlite3.IntegrityError):
            await db.delete_task(task.task_id)
    async with DatabaseManager(db_path, seed_path=False) as db:
        assert await db.get_container("agent") == container
        assert await db.get_capability("claude", "model") == capability
        assert await db.get_task("task") == task
        assert await db.get_event(event.event_id) == event
        assert await db.delete_event(event.event_id)
        assert await db.delete_task(task.task_id)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_seed_load_is_idempotent(db_path, tmp_path):
    seed = tmp_path / "seed.yaml"
    seed.write_text(
        "containers:\n- container_id: seeded\n  name: Seed\n  role: coder\n  provider: claude\n",
        encoding="utf-8",
    )
    async with DatabaseManager(db_path, seed_path=seed) as db:
        agent = await db.get_container("seeded")
        agent.name = "Changed"
        await db.upsert_container(agent)
    async with DatabaseManager(db_path, seed_path=seed) as db:
        assert len(await db.list_containers()) == 1
        assert (await db.get_container("seeded")).name == "Changed"
