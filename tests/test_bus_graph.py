import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from dagger.bus import EventBus, FileBus
from dagger.graph import build_graph


@pytest.mark.asyncio
async def test_event_bus_broadcast_isolated_snapshots():
    bus = EventBus()
    a, b = await bus.subscribe(), await bus.subscribe()
    event = {"nodes": [{"id": "a"}], "edges": []}
    await bus.set_graph(event)
    event["nodes"].clear()
    first, second = await a.get(), await b.get()
    first["nodes"].clear()
    assert second["nodes"] == [{"id": "a"}]
    assert (await bus.get_graph())["nodes"] == [{"id": "a"}]
    await bus.unsubscribe(a)
    await bus.publish({"event": "next"})
    assert a.empty()
    assert await b.get() == {"event": "next"}


def test_file_bus_atomic_and_rejects_traversal():
    import shutil
    import uuid
    from pathlib import Path

    directory = Path(".test-bus-" + uuid.uuid4().hex)
    try:
        bus = FileBus(directory)
        bus.write("task.json", {"value": 1})
        bus.write("task.json", {"value": 2})
        assert bus.read("task.json") == {"value": 2}
        assert [p.name for p in directory.iterdir()] == ["task.json"]
        with pytest.raises(ValueError):
            bus.write("../escape", {})
        (directory / "link").symlink_to("task.json")
        with pytest.raises(OSError):
            bus.read("link")
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.asyncio
async def test_fanout_remediation_dependency_and_review_resume():
    first_batch = asyncio.Event()
    started = []
    calls = []

    async def planner(_state):
        return {"milestones": [{"milestone_id": "a"}, {"milestone_id": "b"},
                               {"milestone_id": "c"}],
                "dependencies": {"c": ["a", "b"]}}

    async def worker(state):
        key = state["task"]["milestone_id"]
        started.append(key)
        calls.append((key, state["task"]["_hop_count"]))
        if key in {"a", "b"} and state["task"]["_hop_count"] == 0:
            if len(started) == 2:
                first_batch.set()
            await asyncio.wait_for(first_batch.wait(), 1)
        return "ok"

    async def auditor(task, hop):
        failed = task["milestone_id"] == "a" and hop == 0
        return {"status": "DOD_FAILED" if failed else "DOD_PASSED",
                "remediation_brief": "fix", "human_review_required": False}

    graph = build_graph(planner, worker, auditor, checkpointer=InMemorySaver(), max_hops=2)
    config = {"configurable": {"thread_id": "test"}}
    await graph.ainvoke({"run_id": "test", "kanban_id": "board", "goal": "goal"}, config)
    checkpoint = await graph.aget_state(config)
    assert checkpoint.next == ("human_review_gate",)
    assert calls == [("a", 0), ("b", 0), ("a", 1), ("c", 0)]
    assert checkpoint.values["dispatch"] == []
    await graph.ainvoke(None, config, context={"decision": True})
    assert (await graph.aget_state(config)).values["status"] == "Done"


@pytest.mark.asyncio
async def test_failed_audit_cannot_be_approved_as_done():
    async def planner(_):
        return {"milestones": [{"milestone_id": "a"}], "dependencies": {}}

    async def worker(_):
        return "ok"

    async def auditor(_, hop):
        return {"status": "DOD_FAILED", "human_review_required": False}

    graph = build_graph(planner, worker, auditor, max_hops=2)
    config = {"configurable": {"thread_id": "failed"}}
    await graph.ainvoke({"run_id": "failed", "kanban_id": "k", "goal": "g"}, config)
    assert len((await graph.aget_state(config)).values["results"]) == 2
    await graph.ainvoke(None, config, context={"decision": True})
    assert (await graph.aget_state(config)).values["status"] == "Failed"


@pytest.mark.asyncio
async def test_worker_failure_overrides_passing_audit():
    async def planner(_):
        return {"milestones": [{"milestone_id": "a"}], "dependencies": {}}

    async def worker(_):
        raise RuntimeError("provider failed")

    async def auditor(_, hop):
        return {"status": "DOD_PASSED"}

    graph = build_graph(planner, worker, auditor, max_hops=1)
    config = {"configurable": {"thread_id": "worker-error"}}
    await graph.ainvoke({"run_id": "failed", "kanban_id": "k", "goal": "g"}, config)
    state = await graph.aget_state(config)
    assert state.values["audits"]["a"]["status"] == "DOD_FAILED"
    await graph.ainvoke(None, config, context={"decision": True})
    assert (await graph.aget_state(config)).values["status"] == "Failed"


@pytest.mark.asyncio
async def test_final_audit_detects_regression():
    checks = {"a": 0}
    calls = []

    async def planner(_):
        return {"milestones": [{"milestone_id": "a"}, {"milestone_id": "b"}],
                "dependencies": {"b": ["a"]}}

    async def worker(state):
        calls.append(state["task"]["milestone_id"])
        return "ok"

    async def auditor(task, _hop):
        if task["milestone_id"] == "a":
            checks["a"] += 1
            # First final audit catches a regression caused by b.
            failed = checks["a"] == 2
        else:
            failed = False
        return {"status": "DOD_FAILED" if failed else "DOD_PASSED", "human_review_required": False}

    graph = build_graph(planner, worker, auditor, max_hops=2)
    config = {"configurable": {"thread_id": "regression"}}
    await graph.ainvoke({"run_id": "r", "kanban_id": "k", "goal": "g"}, config)
    assert calls == ["a", "b", "a"]
    assert (await graph.aget_state(config)).next == ("human_review_gate",)
    assert (await graph.aget_state(config)).values["audits"]["a"]["status"] == "DOD_PASSED"


@pytest.mark.asyncio
async def test_studio_graph_initializes_missing_ids(monkeypatch):
    import dagger.graph as module

    class Database:
        async def get_task(self, _):
            return object()

    class Studio:
        db = Database()

        async def _emit(self, *args):
            pass

        async def _plan(self, state):
            assert state["run_id"] and state["kanban_id"]
            return {"milestones": [{"milestone_id": "one"}], "dependencies": {}}

        async def _work(self, state):
            return "ok"

        async def _audit(self, task, hop):
            return {"status": "DOD_PASSED"}

    monkeypatch.setattr(module, "_studio_instance", Studio())
    config = {"configurable": {"thread_id": "studio-test"}}
    await module.graph.ainvoke({"goal": "Studio goal"}, config)
    assert (await module.graph.aget_state(config)).next == ("human_review_gate",)


@pytest.mark.asyncio
async def test_studio_executor_constructs_valid_envelope(monkeypatch):
    import dagger.graph as module
    import dagger.db
    import dagger.providers
    import dagger.agents.orchestrator
    from dagger.schema import AgentContainer

    seen = []

    class Database:
        pass

    class Provider:
        async def run(self, container, task):
            seen.append(task)
            return "planned"

    class Registry:
        def get(self, name):
            return Provider()

    class App:
        def __init__(self, db, registry, manager):
            self.manager = manager

        async def select(self, milestone):
            return AgentContainer(name="worker", role="worker", provider="api")

    monkeypatch.setattr(module, "_studio_instance", None)
    monkeypatch.setattr(dagger.db, "DatabaseManager", Database)
    monkeypatch.setattr(dagger.providers, "ProviderRegistry", Registry)
    monkeypatch.setattr(dagger.agents.orchestrator, "Orchestrator", App)
    app = await module._studio()
    assert await app.manager.planner("plan") == "planned"
    assert seen[0].dod.acceptance_criteria == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_worker_handoff_renames_staging_to_outbox(monkeypatch, fails):
    from pathlib import Path
    from unittest.mock import AsyncMock, MagicMock

    import dagger.agents.orchestrator as module
    from dagger.schema import AgentContainer

    app = module.Orchestrator.__new__(module.Orchestrator)
    app.worktree = Path.cwd()
    app.max_hops = 3
    for name in ("inbox", "tmp", "outbox", "archive"):
        bus = MagicMock()
        bus._path.side_effect = lambda filename, directory=name: Path("bus") / directory / filename
        setattr(app, name, bus)
    app.select = AsyncMock(return_value=AgentContainer(name="worker", role="worker", provider="api"))
    app._emit = AsyncMock()
    app._update_milestone = AsyncMock()
    app._run_provider = AsyncMock(side_effect=RuntimeError("failed") if fails else None,
                                  return_value="result")
    moves = []
    monkeypatch.setattr(module.os, "replace", lambda source, target: moves.append((source, target)))
    state = {"run_id": "run", "kanban_id": "board", "task": {
        "milestone_id": "milestone", "title": "Task", "dod": {"acceptance_criteria": []},
        "_hop_count": 0,
    }}
    if fails:
        with pytest.raises(RuntimeError, match="failed"):
            await app._work(state)
    else:
        assert await app._work(state) == "result"
    name, payload = app.tmp.write.call_args.args
    assert (Path("bus/tmp") / name, Path("bus/outbox") / name) in moves
    assert payload["error" if fails else "result"] == ("failed" if fails else "result")


@pytest.mark.asyncio
async def test_event_bus_stamps_ai_nodes_and_reserves_system_for_control_gates():
    bus = EventBus()
    q = await bus.subscribe()

    for target, provider, model in (
        ("pm_plan_node", "claude", "claude-sonnet-4-5"),
        ("orchestrator", "codex", "gpt-5-codex"),
        ("worker:m1", "agy", "gemini-4-hydrogen"),
        ("dod_audit_node", "agy", "gemini-4-neon"),
    ):
        await bus.publish({
            "run_id": "run-1",
            "target_node": target,
            "provider": provider,
            "metadata": {"model": model},
        })
        _ = await q.get()

    await bus.set_graph({
        "nodes": [
            {"id": "run-1:initialize", "data": {"label": "initialize"}},
            {"id": "run-1:pm_plan_node", "data": {"label": "pm_plan_node"}},
            {"id": "run-1:orchestrator", "data": {"label": "orchestrator"}},
            {"id": "run-1:worker:m1", "data": {"label": "worker:m1"}},
            {"id": "run-1:dod_audit_node", "data": {"label": "dod_audit_node"}},
            {"id": "run-1:human_review_gate", "data": {"label": "human_review_gate"}},
            {"id": "run-1:finalize", "data": {"label": "finalize"}},
        ],
        "edges": [],
    })
    graph = await q.get()
    by_label = {n["data"]["label"]: n["data"] for n in graph["nodes"]}

    for ai_node in ("pm_plan_node", "orchestrator", "worker:m1", "dod_audit_node"):
        assert by_label[ai_node]["provider"] != "system"
        assert by_label[ai_node]["model"]
    assert by_label["worker:m1"]["provider"] == "agy"
    assert by_label["worker:m1"]["model"] == "gemini-4-hydrogen"

    for gate in ("initialize", "human_review_gate", "finalize"):
        assert by_label[gate]["provider"] == "system"

    await bus.update_node({"id": "run-1:human_review_gate", "data": {"label": "human_review_gate", "provider": "claude"}})
    updated = await q.get()
    gate_node = next(n for n in updated["nodes"] if n["id"] == "run-1:human_review_gate")
    assert gate_node["data"]["provider"] == "system"


def test_agy_cli_runner_arguments_and_parse():
    from dagger.providers.cli_runners import AgyCliRunner
    from dagger.schema import AgentContainer

    runner = AgyCliRunner()
    container = AgentContainer(name="fast_auditor_agy", role="fast_auditor", provider="agy", model="gemini-4-hydrogen")
    args = runner.arguments(container, "audit this", None)
    assert args == ["agy", "-p", "audit this", "--model", "gemini-4-hydrogen-low", "--disable-slash-commands"]
    assert "--effort" not in args
    assert runner.parse("  ok  ", None) == "ok"


def test_strict_gemini_auth_gating(tmp_path, monkeypatch):
    from dagger.agents.generator import _has_gemini_auth

    for var in ("GEMINI_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "GOOGLE_CLOUD_PROJECT", "GOOGLE_GENAI_USE_GCA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    assert _has_gemini_auth() is False
    gemini_dir = tmp_path / ".gemini"
    gemini_dir.mkdir()
    # Stale oauth_creds.json without settings.json must return False.
    (gemini_dir / "oauth_creds.json").write_text("{}", encoding="utf-8")
    assert _has_gemini_auth() is False

    (gemini_dir / "settings.json").write_text('{"selectedType": "oauth-personal"}', encoding="utf-8")
    assert _has_gemini_auth() is True

    monkeypatch.delenv("GOOGLE_GENAI_USE_GCA", raising=False)
    (gemini_dir / "settings.json").unlink()
    monkeypatch.setenv("GOOGLE_GENAI_USE_GCA", "true")
    assert _has_gemini_auth() is True


@pytest.mark.asyncio
async def test_orchestrator_run_provider_falls_back_across_registry():
    from unittest.mock import AsyncMock
    import dagger.agents.orchestrator as module
    from dagger.schema import AgentContainer, DefinitionOfDone, TaskEnvelope, DefinitionOfDone

    app = module.Orchestrator.__new__(module.Orchestrator)
    app._container_limits = {}
    app._last_executed_containers = {}
    app._emit = AsyncMock()
    app._update_milestone = AsyncMock()

    c1 = AgentContainer(container_id="c1", name="c1", role="worker", provider="gemini", active=True, fallback_providers=[])
    c2 = AgentContainer(container_id="c2", name="c2", role="worker", provider="agy", active=True, fallback_providers=[])

    class DB:
        async def list_containers(self):
            return [c1, c2]
        async def list_capabilities(self):
            return []

    class Provider:
        def __init__(self, should_fail):
            self.should_fail = should_fail
        async def run(self, container, envelope, usage_context=None):
            if self.should_fail:
                from dagger.providers.base import ProviderError
                raise ProviderError("gemini exited with status 41: auth required")
            return "recovered"

    class Registry:
        def get(self, name):
            return Provider(name == "gemini")

    app.db = DB()
    app.registry = Registry()
    env = TaskEnvelope(
        task_id="t1", run_id="r1", kanban_id="k1", container_id="c1",
        parent_task_id="m1", prompt="go", dod=DefinitionOfDone(acceptance_criteria=[]),
    )
    assert await app._run_provider(c1, env) == "recovered"


@pytest.mark.asyncio
async def test_agy_probing_active_gating_and_oauth_isolation(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from dagger.agents.generator import AgentGenerator
    from dagger.db import DatabaseManager
    from dagger.providers import AgyCliRunner, ProviderRegistry

    assert isinstance(ProviderRegistry().get("agy"), AgyCliRunner)
    assert "GEMINI_API_KEY" in AgyCliRunner.stripped_env
    assert "GOOGLE_API_KEY" in AgyCliRunner.stripped_env

    async with DatabaseManager(tmp_path / "db.sqlite", seed_path=False) as db:
        gen = AgentGenerator(db=db, export_dir=tmp_path / "exports", job_store_path=tmp_path / "jobs.sqlite")
        monkeypatch.setattr("dagger.agents.generator.shutil.which", lambda _cmd: None)
        monkeypatch.setattr(gen, "_probe_local", AsyncMock(return_value=[]))
        monkeypatch.setattr(gen, "discover_web", AsyncMock(return_value=[]))
        containers = await gen.refresh_catalog()
        by_id = {c.container_id: c for c in containers}
        assert by_id["fast_auditor_agy"].active is False
        assert by_id["repo_auditor_agy"].active is False

        seen_cmds = []

        async def fake_cmd(exe, *args):
            seen_cmds.append((exe, args))
            return True, "1.5.14"

        monkeypatch.setattr("dagger.agents.generator.shutil.which", lambda cmd: f"/bin/{cmd}" if cmd == "agy" else None)
        monkeypatch.setattr(gen, "_command", fake_cmd)
        containers = await gen.refresh_catalog()
        by_id = {c.container_id: c for c in containers}
        assert by_id["fast_auditor_agy"].active is True
        assert by_id["repo_auditor_agy"].active is True
        assert all(args == ("--version",) for _, args in seen_cmds)


@pytest.mark.asyncio
async def test_orchestrator_emit_stamps_real_provider_and_model_on_ai_nodes():
    from unittest.mock import AsyncMock
    import dagger.agents.orchestrator as module
    from dagger.schema import AgentContainer

    app = module.Orchestrator.__new__(module.Orchestrator)
    app.bus = EventBus()
    app.db = AsyncMock()
    app.db.list_containers = AsyncMock(return_value=[
        AgentContainer(container_id="pm_c", name="PM", role="pm", provider="claude", model="claude-sonnet-4-5", active=True),
    ])
    app.project_manager = type("PM", (), {"provider": "claude", "model": "claude-sonnet-4-5"})()

    await app._emit("r1", "k1", "NODE_SPAWNED", {"node": "pm_plan_node"})
    await app._emit("r1", "k1", "NODE_SPAWNED", {"node": "orchestrator"})
    await app._emit("r1", "k1", "NODE_SPAWNED", {"node": "container_worker_node", "milestone_id": "m1", "provider": "agy", "model": "gemini-4-hydrogen"})
    await app._emit("r1", "k1", "NODE_SPAWNED", {"node": "dod_audit_node", "milestone_id": "m1"})
    await app._emit("r1", "k1", "COMPLETED", {"node": "human_review_gate"})

    graph = await app.bus.get_graph()
    by_label = {n["data"]["label"]: n["data"] for n in graph["nodes"]}
    assert by_label["pm_plan_node"]["provider"] == "claude"
    assert by_label["pm_plan_node"]["model"] == "claude-sonnet-4-5"
    assert by_label["orchestrator"]["provider"] == "claude"
    assert by_label["worker:m1"]["provider"] == "agy"
    assert by_label["worker:m1"]["model"] == "gemini-4-hydrogen"
    assert by_label["dod_audit_node"]["provider"] == "claude"
    assert by_label["human_review_gate"]["provider"] == "system"


@pytest.mark.asyncio
async def test_event_bus_card_log_does_not_mutate_graph_or_metadata():
    from dagger.bus import EventBus
    from dagger.schema import CardLogEntry

    bus = EventBus()
    q = await bus.subscribe()
    entry = CardLogEntry(
        kanban_id="k1",
        run_id="r1",
        node_id="agent:codex",
        provider="codex",
        channel="stdout",
        text="hello world",
    ).model_dump(mode="json")
    await bus.publish_card_log(entry)

    received = q.get_nowait()
    assert received["event_type"] == "CARD_LOG"
    assert received["text"] == "hello world"
    assert await bus.get_graph() == {"nodes": [], "edges": []}
    assert getattr(bus, "_node_metadata", {}) == {}


@pytest.mark.asyncio
async def test_db_append_and_list_card_logs_bounded_pruning(tmp_path):
    from dagger.db import DatabaseManager

    db = DatabaseManager(tmp_path / "test.db", seed_path=False)
    await db.initialize()
    try:
        for i in range(5):
            await db.append_card_log(
                {
                    "log_id": f"l{i}",
                    "kanban_id": "k1",
                    "run_id": "r1",
                    "node_id": "orchestrator",
                    "provider": "system",
                    "channel": "system",
                    "text": f"msg-{i}",
                    "ts": f"2026-11-24T00:00:0{i}Z",
                },
                max_per_run=3,
            )
        logs = await db.list_card_logs("k1", run_id="r1", limit=10)
        assert [row["log_id"] for row in logs] == ["l2", "l3", "l4"]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_api_get_task_logs_replay_and_404(tmp_path):
    from fastapi.testclient import TestClient
    from dagger.server import create_app
    from dagger.schema import KanbanTask

    app = create_app(db_path=tmp_path / "api.db", container_dir=tmp_path / "containers")
    with TestClient(app) as client:
        assert client.get("/api/tasks/missing/logs").status_code == 404

        task = KanbanTask(task_id="t1", title="T1", description="D1", run_id="r1")
        await app.state.db.upsert_task(task)
        await app.state.db.append_card_log(
            {
                "log_id": "log-1",
                "kanban_id": "t1",
                "run_id": "r1",
                "node_id": "pm_plan_node",
                "provider": "claude",
                "channel": "dialogue",
                "text": "planned",
                "ts": "2026-11-24T00:00:00Z",
            }
        )
        await app.state.db.append_card_log(
            {
                "log_id": "log-2",
                "kanban_id": "t1",
                "run_id": "r2",
                "node_id": "orchestrator",
                "provider": "system",
                "channel": "system",
                "text": "other run",
                "ts": "2026-11-24T00:00:01Z",
            }
        )

        resp = client.get("/api/tasks/t1/logs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "t1"
        assert body["run_id"] == "r1"
        assert [l["log_id"] for l in body["logs"]] == ["log-1", "log-2"]

        resp_filtered = client.get("/api/tasks/t1/logs?run_id=r1")
        assert resp_filtered.status_code == 200
        assert [l["log_id"] for l in resp_filtered.json()["logs"]] == ["log-1"]


@pytest.mark.asyncio
async def test_cli_runner_streams_stdout_stderr_lines_live(tmp_path):
    import sys
    from dagger.providers.base import card_log_hook
    from dagger.providers.cli_runners import ClaudeCliRunner
    from dagger.schema import AgentContainer, DefinitionOfDone, TaskEnvelope

    class EchoCLI(ClaudeCliRunner):
        executable = sys.executable

        def arguments(self, container, prompt, output):
            return [
                sys.executable,
                "-c",
                "import sys; print('out-1'); print('err-1', file=sys.stderr); print('out-2')",
            ]

        def parse(self, text, output):
            return text

    runner = EchoCLI()
    container = AgentContainer(container_id="c1", name="C1", role="coder", provider="codex")
    envelope = TaskEnvelope(task_id="m1", run_id="r1", kanban_id="k1", container_id="c1", parent_task_id="m1", prompt="Do", dod=DefinitionOfDone(acceptance_criteria=[]))

    seen: list[tuple[str, str]] = []

    async def hook(channel: str, line: str) -> None:
        seen.append((channel, line))

    token = card_log_hook.set(hook)
    try:
        out = await runner.run(container, envelope)
    finally:
        card_log_hook.reset(token)

    assert "out-1" in out and "out-2" in out
    assert ("stdout", "out-1") in seen
    assert ("stdout", "out-2") in seen
    assert ("stderr", "err-1") in seen


@pytest.mark.asyncio
async def test_orchestrator_emits_dialogue_and_dod_command_card_logs(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from dagger.agents.orchestrator import Orchestrator
    from dagger.agents.project_manager import CommandEvidence
    from dagger.bus import EventBus
    from dagger.db import DatabaseManager
    from dagger.schema import DefinitionOfDone, MilestoneTask

    db = DatabaseManager(tmp_path / "orch.db", seed_path=False)
    await db.initialize()
    bus = EventBus()
    try:
        orch = Orchestrator(db=db, bus=bus, registry=None, project_manager=None)
        orch.project_manager = type(
            "PM",
            (),
            {
                "provider": "claude",
                "model": "claude-sonnet-4-5",
                "audit_milestone": AsyncMock(
                    return_value=SimpleNamespace(
                        milestone_id="m1",
                        status="DOD_PASSED",
                        passed=True,
                        hop_count=1,
                        max_hops=3,
                        criteria=(),
                        artifacts=(),
                        commands=(CommandEvidence("pytest -q", True, 0, "1 passed", ""),),
                        failures=(),
                        model_dump=lambda mode=None: {"status": "DOD_PASSED"},
                    )
                ),
            },
        )()

        milestone = MilestoneTask(
            milestone_id="m1",
            title="M1",
            description="Desc",
            dod=DefinitionOfDone(acceptance_criteria=["tests pass"], verification_commands=["pytest -q"]),
        )
        task_dict = milestone.model_dump(mode="json")
        task_dict["_kanban_id"] = "k1"
        task_dict["_run_id"] = "r1"
        await orch._audit(task_dict, hop_count=0)

        logs = await db.list_card_logs("k1", run_id="r1")
        channels = [l["channel"] for l in logs]
        assert "command" in channels
        assert "stdout" in channels
        assert "dialogue" in channels
        cmd_log = next(l for l in logs if l["channel"] == "command")
        assert "pytest -q" in cmd_log["text"]
        assert "exit=0" in cmd_log["text"]
    finally:
        await db.close()
