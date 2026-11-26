import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dagger import server
from dagger.agents.orchestrator import Orchestrator
from dagger.agents.project_manager import ProjectManager
from dagger.db import DatabaseManager
from dagger.providers import BaseProvider, ProviderError, ProviderRateLimitError, ProviderRegistry
from dagger.schema import (
    AgentContainer,
    DefinitionOfDone,
    EndpointCapability,
    KanbanTask,
    MilestoneTask,
    TaskEnvelope,
)

ROOT = Path(__file__).resolve().parents[1]


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


@pytest.mark.asyncio
async def test_read_only_repo_auditor_materializes_pm_approved_artifact_and_injects_context(tmp_path):
    worktree = tmp_path / "repo"
    (worktree / "src").mkdir(parents=True)
    (worktree / "README.md").write_text("# DAGGER\n", encoding="utf-8")
    (worktree / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    # Lockfiles and ignored dirs must be excluded from injected context_files.
    (worktree / "uv.lock").write_text("lock-data\n", encoding="utf-8")
    (worktree / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "site.py").write_text("# ignored\n", encoding="utf-8")

    seen_calls = []

    class FakeGeminiProvider(BaseProvider):
        def __init__(self):
            super().__init__("gemini")

        async def run(self, container, task):
            payload = json.loads(task.prompt)
            seen_calls.append((container, payload))
            if task.parent_task_id == "audit":
                return "# Repo Audit\nFINDINGS: all checks clean"
            return "# Summary\nAudit OK"

    async def planner(_):
        return json.dumps({
            "milestones": [
                {
                    "milestone_id": "audit",
                    "title": "Audit repository",
                    "description": "Review src/app.py and README.md",
                    "required_skills": ["review", "analysis"],
                    "depends_on": [],
                    "dod": {
                        "acceptance_criteria": [
                            "Audit report has at least 2 lines",
                            "Audit report includes FINDINGS",
                        ],
                        "expected_artifacts": ["reports/repo_audit.md"],
                        "verification_commands": [
                            'test "$(awk \'END{print NR}\' reports/repo_audit.md)" -ge 2',
                            "grep -q FINDINGS reports/repo_audit.md",
                        ],
                    },
                },
                {
                    "milestone_id": "summary",
                    "title": "Summarize audit",
                    "description": "Summarize reports/repo_audit.md",
                    "required_skills": ["review", "analysis"],
                    "depends_on": ["audit"],
                    "dod": {
                        "acceptance_criteria": ["Summary confirms Audit OK"],
                        "expected_artifacts": ["reports/summary.md"],
                        "verification_commands": ["grep -q 'Audit OK' reports/summary.md"],
                    },
                },
            ]
        })

    registry = ProviderRegistry(defaults=False)
    registry.register("gemini", FakeGeminiProvider())

    seed_path = ROOT / "containers" / "seed_containers.yaml"
    async with DatabaseManager(tmp_path / "board.sqlite", seed_path=seed_path) as db:
        auditor = await db.get_container("repo_auditor_gemini")
        architect = await db.get_container("architect_claude")
        assert auditor is not None and auditor.allowed_tools == ["read", "search"]
        assert architect is not None and architect.allowed_tools == ["read", "search"]

        pm = ProjectManager(planner=planner, workspace=worktree)
        orchestrator = Orchestrator(
            db, registry, pm, worktree_path=worktree, bus_path=tmp_path / "bus"
        )
        state = await orchestrator.start("run-ro-audit", "Audit repository")

        assert state["next"] == ["human_review_gate"]
        assert state["audits"]["audit"]["status"] == "DOD_PASSED"
        assert state["audits"]["summary"]["status"] == "DOD_PASSED"

        # Output was materialized to PM-approved paths without giving the auditor write tools.
        audit_file = worktree / "reports" / "repo_audit.md"
        summary_file = worktree / "reports" / "summary.md"
        assert audit_file.read_text(encoding="utf-8") == "# Repo Audit\nFINDINGS: all checks clean\n"
        assert summary_file.read_text(encoding="utf-8") == "# Summary\nAudit OK\n"

        assert len(seen_calls) == 2
        first_container, first_payload = seen_calls[0]
        assert first_container.container_id == "fast_auditor_gemini"
        assert first_container.allowed_tools == ["read", "search"]
        assert "README.md" in first_payload["context_files"]
        assert "src/app.py" in first_payload["context_files"]
        assert "uv.lock" not in first_payload["context_files"]
        assert "package-lock.json" not in first_payload["context_files"]
        assert all(not path.startswith(".venv/") for path in first_payload["context_files"])
        assert first_payload["target_paths"] == ["reports/repo_audit.md"]
        assert first_payload["allowed_write_paths"] == ["reports/repo_audit.md"]

        # Persisted container still has strictly read-only tools and milestones carry allowed_write_paths.
        persisted_auditor = await db.get_container("fast_auditor_gemini")
        assert persisted_auditor.allowed_tools == ["read", "search"]
        persisted_task = await db.get_task("run-ro-audit")
        assert [m.allowed_write_paths for m in persisted_task.milestones] == [
            ["reports/repo_audit.md"],
            ["reports/summary.md"],
        ]


@pytest.mark.asyncio
async def test_read_only_materialization_rejects_path_traversal_and_symlink_escapes(tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (worktree / "escape_link").symlink_to(outside_dir)

    class StaticProvider(BaseProvider):
        def __init__(self):
            super().__init__("gemini")

        async def run(self, container, task):
            return "sensitive output"

    registry = ProviderRegistry(defaults=False)
    registry.register("gemini", StaticProvider())

    seed_path = ROOT / "containers" / "seed_containers.yaml"
    async with DatabaseManager(tmp_path / "board.sqlite", seed_path=seed_path) as db:
        await db.upsert_task(KanbanTask(task_id="board-sec", title="Security"))
        orchestrator = Orchestrator(
            db, registry, ProjectManager(workspace=worktree),
            worktree_path=worktree, bus_path=tmp_path / "bus",
        )

        # 1. Relative path traversal (../outside.md)
        traversal_milestone = MilestoneTask(
            milestone_id="m-traversal",
            title="Traversal",
            required_skills=["review", "analysis"],
            dod=DefinitionOfDone(
                acceptance_criteria=["Write outside"],
                expected_artifacts=["../outside.md"],
                verification_commands=["test -s ../outside.md"],
            ),
            allowed_write_paths=["../outside.md"],
        )
        with pytest.raises(ValueError, match="Refusing"):
            await orchestrator._work({
                "run_id": "run-sec",
                "kanban_id": "board-sec",
                "task": {**traversal_milestone.model_dump(mode="json"), "_hop_count": 0},
            })
        assert not (tmp_path / "outside.md").exists()

        # 2. Absolute path outside worktree
        abs_target = tmp_path / "abs_outside.md"
        abs_milestone = MilestoneTask(
            milestone_id="m-abs",
            title="Absolute outside",
            required_skills=["review", "analysis"],
            dod=DefinitionOfDone(
                acceptance_criteria=["Write outside"],
                expected_artifacts=[str(abs_target)],
                verification_commands=["true"],
            ),
            allowed_write_paths=[str(abs_target)],
        )
        with pytest.raises(ValueError, match="Refusing"):
            await orchestrator._work({
                "run_id": "run-sec",
                "kanban_id": "board-sec",
                "task": {**abs_milestone.model_dump(mode="json"), "_hop_count": 0},
            })
        assert not abs_target.exists()

        # 3. Symlink parent escape outside worktree
        symlink_milestone = MilestoneTask(
            milestone_id="m-symlink",
            title="Symlink escape",
            required_skills=["review", "analysis"],
            dod=DefinitionOfDone(
                acceptance_criteria=["Write through symlink"],
                expected_artifacts=["escape_link/escaped.md"],
                verification_commands=["true"],
            ),
            allowed_write_paths=["escape_link/escaped.md"],
        )
        with pytest.raises(ValueError, match="Refusing"):
            await orchestrator._work({
                "run_id": "run-sec",
                "kanban_id": "board-sec",
                "task": {**symlink_milestone.model_dump(mode="json"), "_hop_count": 0},
            })
        assert not (outside_dir / "escaped.md").exists()

        # 4. Expected artifact not authorized in allowed_write_paths
        unauth_milestone = MilestoneTask(
            milestone_id="m-unauth",
            title="Unauthorized artifact",
            required_skills=["review", "analysis"],
            dod=DefinitionOfDone(
                acceptance_criteria=["Write unauthorized"],
                expected_artifacts=["reports/unapproved.md"],
                verification_commands=["true"],
            ),
            allowed_write_paths=["reports/approved.md"],
        )
        with pytest.raises(ValueError, match="not authorized"):
            await orchestrator._work({
                "run_id": "run-sec",
                "kanban_id": "board-sec",
                "task": {**unauth_milestone.model_dump(mode="json"), "_hop_count": 0},
            })
        assert not (worktree / "reports" / "unapproved.md").exists()


@pytest.mark.asyncio
async def test_orchestrator_duplicate_dod_failure_short_circuits_before_third_hop(tmp_path):
    worktree = tmp_path / "repo"
    worktree.mkdir()
    worker_calls = []

    class NoOpProvider(BaseProvider):
        def __init__(self):
            super().__init__("gemini")

        async def run(self, container, task):
            worker_calls.append((task.parent_task_id, task.hop_count))
            return ""

    async def planner(_):
        return json.dumps({
            "milestones": [
                {
                    "milestone_id": "m1",
                    "title": "Produce audit",
                    "required_skills": ["review", "analysis"],
                    "depends_on": [],
                    "dod": {
                        "acceptance_criteria": ["Audit report exists"],
                        "expected_artifacts": ["reports/missing.md"],
                        "verification_commands": ["test -s reports/missing.md"],
                    },
                },
                {
                    "milestone_id": "m2",
                    "title": "Follow-up",
                    "required_skills": ["review", "analysis"],
                    "depends_on": ["m1"],
                    "dod": {
                        "acceptance_criteria": ["Follow-up exists"],
                        "expected_artifacts": ["reports/followup.md"],
                        "verification_commands": ["test -s reports/followup.md"],
                    },
                },
            ]
        })

    registry = ProviderRegistry(defaults=False)
    registry.register("gemini", NoOpProvider())

    seed_path = ROOT / "containers" / "seed_containers.yaml"
    async with DatabaseManager(tmp_path / "board.sqlite", seed_path=seed_path) as db:
        pm = ProjectManager(planner=planner, workspace=worktree, max_hops=3)
        orchestrator = Orchestrator(
            db, registry, pm, max_hops=3, worktree_path=worktree, bus_path=tmp_path / "bus"
        )
        state = await orchestrator.start("run-dup-stop", "Stop on duplicate DoD failure")

        # Must stop after hop 2 (attempts 0 and 1) and never invoke a 3rd LLM call.
        assert worker_calls == [("m1", 0), ("m1", 1)]
        assert state["attempts"]["m1"] == 2
        assert "m2" not in state["attempts"]
        assert state["audits"]["m1"]["status"] == "DOD_FAILED"
        assert state["audits"]["m1"]["human_review_required"] is True
        assert "Duplicate failure signature" in state["audits"]["m1"]["remediation_brief"]
        assert state["next"] == ["human_review_gate"]


@pytest.mark.asyncio
async def test_tiered_container_routing_selects_lightweight_for_audit_and_heavy_for_coding(tmp_path):
    seed_path = ROOT / "containers" / "seed_containers.yaml"
    async with DatabaseManager(tmp_path / "board.sqlite", seed_path=seed_path) as db:
        fast = await db.get_container("fast_auditor_gemini")
        assert fast is not None
        assert fast.model == "gemini-3.8-flash"
        assert fast.allowed_tools == ["read", "search"]

        orchestrator = Orchestrator(
            db, ProviderRegistry(defaults=False), ProjectManager(workspace=tmp_path),
            worktree_path=tmp_path, bus_path=tmp_path / "bus",
        )

        # Lightweight review/analysis/triage milestones prefer flash / local_free containers.
        review_milestone = MilestoneTask(
            milestone_id="m-rev",
            title="Review",
            required_skills=["review", "analysis"],
            dod=DefinitionOfDone(acceptance_criteria=["ok"]),
        )
        assert (await orchestrator.select(review_milestone)).container_id == "fast_auditor_gemini"

        triage_milestone = MilestoneTask(
            milestone_id="m-triage",
            title="Triage",
            required_skills=["triage"],
            dod=DefinitionOfDone(acceptance_criteria=["ok"]),
        )
        assert (await orchestrator.select(triage_milestone)).container_id == "local_triage"

        # Heavy coding/patching/architecture milestones reserve heavy models.
        coding_milestone = MilestoneTask(
            milestone_id="m-code",
            title="Code",
            required_skills=["coding", "testing"],
            dod=DefinitionOfDone(acceptance_criteria=["ok"]),
        )
        assert (await orchestrator.select(coding_milestone)).container_id == "tdd_engineer_codex"

        arch_milestone = MilestoneTask(
            milestone_id="m-arch",
            title="Architecture",
            required_skills=["architecture"],
            dod=DefinitionOfDone(acceptance_criteria=["ok"]),
        )
        assert (await orchestrator.select(arch_milestone)).container_id == "architect_claude"
