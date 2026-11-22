import json
import time

import pytest
from fastapi.testclient import TestClient

from dagger import server
from dagger.agents.orchestrator import Orchestrator
from dagger.db import DatabaseManager
from dagger.providers import BaseProvider, ProviderError, ProviderRateLimitError, ProviderRegistry
from dagger.schema import AgentContainer, DefinitionOfDone, EndpointCapability, KanbanTask, TaskEnvelope


def test_api_run_survives_restart_and_human_approval(tmp_path, monkeypatch):
    class FakeProvider(BaseProvider):
        def __init__(self):
            super().__init__("api")

        async def run(self, container, task):
            if "Decompose this project" in task.prompt:
                dod = {
                    "acceptance_criteria": ["README exists"],
                    "expected_artifacts": ["README.md"],
                    "verification_commands": ["test -s README.md"],
                }
                return json.dumps(
                    {
                        "milestones": [
                            {"milestone_id": "first", "title": "First", "dod": dod},
                            {
                                "milestone_id": "second",
                                "title": "Second",
                                "depends_on": ["first"],
                                "dod": dod,
                            },
                        ]
                    }
                )
            return "implemented"

    class Registry(ProviderRegistry):
        def __init__(self):
            super().__init__()
            self.register("api", FakeProvider())

    class Generator:
        def __init__(self, db, **kwargs):
            self.db = db

        async def refresh_catalog(self):
            for container in await self.db.list_containers():
                container.active = False
                await self.db.upsert_container(container)
            await self.db.upsert_container(
                AgentContainer(
                    container_id="fake", name="Fake", role="worker", provider="api", model="fake"
                )
            )
            await self.db.upsert_capability(
                EndpointCapability(provider="api", model_name="fake", healthy=True)
            )

        def start(self):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(server, "ProviderRegistry", Registry)
    app = server.create_app(
        tmp_path / "board.sqlite",
        tmp_path / "checkpoints.sqlite",
        generator_factory=Generator,
        container_dir=tmp_path / "definitions",
    )
    with TestClient(app) as client:
        response = client.post("/api/tasks", json={"title": "Integration", "auto_start": True})
        assert response.status_code == 201
        task = response.json()
        run_id = task["run_id"]
        for _ in range(100):
            state = client.get(f"/api/runs/{run_id}").json()
            board = client.get("/api/tasks").json()
            if board[0]["status"] in {"Review", "Failed"}:
                break
            time.sleep(0.05)
        assert board[0]["status"] == "Review"
        assert state["next"] == ["human_review_gate"]
        assert [result["milestone_id"] for result in state["results"]] == ["first", "second"]
        assert all(report["status"] == "DOD_PASSED" for report in state["audits"].values())
        assert all(milestone["status"] == "passed" for milestone in board[0]["milestones"])
        archive = tmp_path / "bus" / "archive"
        outbox = tmp_path / "bus" / "outbox"
        assert len(list(archive.glob("*.json"))) == 2
        assert len(list(outbox.glob("*.json"))) == 2
        assert not list((tmp_path / "bus" / "inbox").glob("*.json"))
        events = client.get(f"/api/runs/{run_id}/events").json()
        spawned = [event for event in events if event["event_type"] == "NODE_SPAWNED"
                   and event["container_id"] == "fake"]
        assert len(spawned) == 2
        assert all(event["provider"] == "api" and event["target_node"] == "agent:fake"
                   for event in spawned)
        assert any(event["event_type"] == "EDGE_TRAVERSED" and
                   event["target_node"] == "agent:fake" for event in events)

    with TestClient(app) as client:
        assert client.get(f"/api/runs/{run_id}").json()["next"] == ["human_review_gate"]
        replayed = client.get("/api/graph", params={"run_id": run_id}).json()
        assert any(node["id"].endswith(":agent:fake") for node in replayed["nodes"])
        assert replayed["edges"]
        assert client.post(f"/api/tasks/{task['task_id']}/approve").status_code == 202
        for _ in range(100):
            board = client.get("/api/tasks").json()
            if board[0]["status"] in {"Done", "Failed"}:
                break
            time.sleep(0.05)
        assert board[0]["status"] == "Done"
        events = client.get(f"/api/runs/{run_id}/events").json()
        assert any(event["event_type"] == "COMPLETED" for event in events)


@pytest.mark.asyncio
async def test_rate_limit_fallback_uses_persisted_container_and_emits_edge(tmp_path):
    class Runner(BaseProvider):
        def __init__(self, name, error=None):
            super().__init__(name)
            self.error = error
            self.calls = []

        async def run(self, container, task):
            self.calls.append((container, task))
            if self.error:
                raise self.error
            return "fallback result"

    registry = ProviderRegistry(defaults=False)
    primary = Runner("claude", ProviderRateLimitError("limited"))
    fallback = Runner("api")
    registry.register("claude", primary)
    registry.register("api", fallback)
    async with DatabaseManager(tmp_path / "board.sqlite", seed_path=False) as db:
        first = AgentContainer(container_id="first", name="First", role="worker",
                               provider="claude", fallback_providers=["api"])
        second = AgentContainer(container_id="second", name="Second", role="worker",
                                provider="api", model="local-model", base_url="http://localhost:8080")
        await db.upsert_container(first)
        await db.upsert_container(second)
        await db.upsert_task(KanbanTask(task_id="board", title="Board"))
        orchestrator = Orchestrator(db, registry, None, bus_path=tmp_path / "bus")
        envelope = TaskEnvelope(task_id="task", run_id="run", kanban_id="board",
                                parent_task_id="milestone", container_id="first",
                                prompt="work", dod=DefinitionOfDone(acceptance_criteria=[]))
        assert await orchestrator._run_provider(first, envelope) == "fallback result"
        used, sent = fallback.calls[0]
        assert used.container_id == sent.container_id == "second"
        assert used.model == "local-model" and used.base_url == "http://localhost:8080"
        events = await db.list_events(run_id="run")
        event = next(event for event in events if event.event_type == "FALLBACK")
        assert (event.source_node, event.target_node, event.provider, event.container_id) == (
            "agent:first", "agent:second", "api", "second"
        )

        fallback.calls.clear()
        primary.error = ProviderError("not a rate limit")
        with pytest.raises(ProviderError, match="not a rate limit"):
            await orchestrator._run_provider(first, envelope)
        assert not fallback.calls
