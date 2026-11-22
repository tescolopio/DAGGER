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
