import sqlite3

import pytest
from pydantic import ValidationError

from dagger.db import DatabaseManager
from dagger.schema import (
    AgentContainer,
    AvailableModel,
    EndpointCapability,
    GraphEvent,
    KanbanTask,
    ProviderCredentialStatus,
)


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


def test_available_model_and_provider_status_schema_defaults():
    model = AvailableModel(
        provider_id="gemini",
        model_id="gemini-2.5-pro",
        display_name="Gemini 2.5 Pro",
        context_window=1_048_576,
        discovery_source="live",
        last_checked="2026-11-26T16:00:00Z",
    )
    assert model.available is True
    assert model.is_llm is True
    assert model.last_checked.tzinfo is not None

    cap = EndpointCapability(provider="api", model_name="llama-3.3-70b-versatile")
    assert cap.available is True
    assert cap.service_provider is None
    assert cap.display_name is None
    assert cap.is_llm is True
    assert cap.discovery_source == "static"

    status = ProviderCredentialStatus(
        provider_id="local",
        display_name="Local OpenAI-Compatible",
        kind="local",
        status="unreachable",
        supports_base_url=True,
        requires_api_key=False,
        base_url="http://127.0.0.1:11434/v1",
        category="local",
        available_models=[model],
        available_model_count=1,
        discovery_status="error",
        discovery_error="connection refused",
        last_discovered_at="2026-11-26T16:00:00Z",
    )
    assert status.status == "unreachable"
    assert status.last_discovered_at is not None


async def test_provider_models_atomic_replace_filtering_and_persistence(db_path):
    async with DatabaseManager(db_path, seed_path=False) as db:
        await db.replace_provider_models(
            "groq",
            [
                AvailableModel(
                    provider_id="groq",
                    model_id="llama-3.3-70b-versatile",
                    available=True,
                    is_llm=True,
                    discovery_source="live",
                ),
                AvailableModel(
                    provider_id="groq",
                    model_id="whisper-large-v3",
                    available=True,
                    is_llm=False,
                    discovery_source="live",
                ),
                AvailableModel(
                    provider_id="groq",
                    model_id="deprecated-model",
                    available=False,
                    is_llm=True,
                    discovery_source="live",
                ),
            ],
        )
        await db.replace_provider_models(
            "tavily",
            [
                AvailableModel(
                    provider_id="tavily",
                    model_id="tavily-search",
                    available=True,
                    is_llm=False,
                    discovery_source="static",
                ),
            ],
        )

        all_groq = await db.list_provider_models("groq")
        assert [m.model_id for m in all_groq] == [
            "deprecated-model",
            "llama-3.3-70b-versatile",
            "whisper-large-v3",
        ]
        avail_groq = await db.list_provider_models("groq", available_only=True)
        assert [m.model_id for m in avail_groq] == ["llama-3.3-70b-versatile", "whisper-large-v3"]
        llm_avail_groq = await db.list_provider_models("groq", available_only=True, llm_only=True)
        assert [m.model_id for m in llm_avail_groq] == ["llama-3.3-70b-versatile"]

        all_llm_avail = await db.list_provider_models(available_only=True, llm_only=True)
        assert [(m.provider_id, m.model_id) for m in all_llm_avail] == [
            ("groq", "llama-3.3-70b-versatile"),
        ]

        # Replacing groq models removes stale entries without affecting tavily.
        await db.replace_provider_models(
            "GROQ",
            [
                AvailableModel(
                    provider_id="ignored",
                    model_id="llama-3.1-8b-instant",
                    available=True,
                    is_llm=True,
                    discovery_source="live",
                )
            ],
        )
        replaced = await db.list_provider_models("groq")
        assert [(m.provider_id, m.model_id) for m in replaced] == [("groq", "llama-3.1-8b-instant")]
        assert len(await db.list_provider_models("tavily")) == 1

    # Verify persistence across reopen and WAL mode intact.
    async with DatabaseManager(db_path, seed_path=False) as db:
        assert [m.model_id for m in await db.list_provider_models("groq")] == ["llama-3.1-8b-instant"]
        assert [m.model_id for m in await db.list_provider_models("tavily")] == ["tavily-search"]
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
