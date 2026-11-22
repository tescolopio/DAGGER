"""LangGraph workflow with checkpointed fan-out, audits and human review."""

from __future__ import annotations

import asyncio
import operator
from typing import Annotated, Any, Awaitable, Callable, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Send
from uuid import uuid4


class DaggerState(TypedDict, total=False):
    run_id: str
    kanban_id: str
    goal: str
    milestones: list[dict[str, Any]]
    dependencies: dict[str, list[str]]
    dispatch: list[dict[str, Any]]
    attempts: dict[str, int]
    audits: dict[str, dict[str, Any]]
    results: Annotated[list[dict[str, Any]], operator.add]
    status: str
    decision: Any
    final_checked: bool


Planner = Callable[[DaggerState], Awaitable[dict[str, Any]]]
Worker = Callable[[dict[str, Any]], Awaitable[Any]]
Auditor = Callable[[dict[str, Any], int], Awaitable[dict[str, Any]]]
Emitter = Callable[[str, str, str, dict[str, Any]], Awaitable[None]]


def build_graph(
    planner: Planner,
    worker: Worker,
    auditor: Auditor,
    *,
    emit: Emitter | None = None,
    checkpointer: Any = None,
    max_hops: int = 3,
):
    """Compile a real async graph; checkpoint values are plain primitives only."""
    if max_hops < 0:
        raise ValueError("max_hops must be non-negative")
    attempts_limit = max(1, max_hops)

    async def notify(state: dict, node: str, event: str, metadata: dict | None = None) -> None:
        if emit:
            await emit(state["run_id"], state["kanban_id"], event, {"node": node, **(metadata or {})})

    async def pm_plan_node(state: DaggerState) -> dict:
        await notify(state, "pm_plan_node", "NODE_SPAWNED")
        plan = await planner(state)
        milestones = plan["milestones"]
        dependencies = plan.get("dependencies", {})
        await notify(state, "pm_plan_node", "EDGE_TRAVERSED", {"milestones": milestones, "dependencies": dependencies})
        return {"milestones": milestones, "dependencies": dependencies, "attempts": {}, "audits": {},
                "status": "Running"}

    def initialize_node(state: DaggerState) -> dict:
        return {"run_id": state.get("run_id") or str(uuid4()),
                "kanban_id": state.get("kanban_id") or str(uuid4())}

    async def schedule_node(state: DaggerState) -> dict:
        attempts = dict(state.get("attempts", {}))
        audits = state.get("audits", {})
        passed = {key for key, audit in audits.items() if audit.get("status") == "DOD_PASSED"}
        dispatch = []
        for milestone in state["milestones"]:
            key = milestone["milestone_id"]
            if (key in passed or attempts.get(key, 0) >= attempts_limit
                    or audits.get(key, {}).get("human_review_required", False)):
                continue
            if not set(state["dependencies"].get(key, [])).issubset(passed):
                continue
            attempt = attempts.get(key, 0)
            task = dict(milestone)
            task["_hop_count"] = attempt
            task["_remediation"] = audits.get(key, {}).get("remediation_brief", "")
            task["_kanban_id"] = state["kanban_id"]
            task["_run_id"] = state["run_id"]
            attempts[key] = attempt + 1
            dispatch.append(task)
        return {"dispatch": dispatch, "attempts": attempts,
                "status": "Running" if dispatch else "Review",
                "final_checked": False if dispatch else state.get("final_checked", False)}

    def fanout(state: DaggerState):
        if not state["dispatch"]:
            if (not state.get("final_checked") and state["milestones"] and
                    all(state.get("audits", {}).get(m["milestone_id"], {}).get("status") == "DOD_PASSED"
                        for m in state["milestones"])):
                return "final_audit_node"
            return "human_review_gate"
        return [Send("container_worker_node", {
            "task": task, "run_id": state["run_id"], "kanban_id": state["kanban_id"]
        }) for task in state["dispatch"]]

    async def container_worker_node(state: dict) -> dict:
        task = state["task"]
        key = task["milestone_id"]
        await notify(state, "container_worker_node", "NODE_SPAWNED", {"milestone_id": key})
        try:
            output = await worker(state)
            result = {"milestone_id": key, "hop_count": task["_hop_count"], "output": output, "error": None}
        except Exception as exc:
            result = {"milestone_id": key, "hop_count": task["_hop_count"], "output": None, "error": str(exc)}
        await notify(state, "container_worker_node", "FAILED" if result["error"] else "EDGE_TRAVERSED",
                     {"milestone_id": key, "error": result["error"]})
        return {"results": [result]}

    async def dod_audit_node(state: DaggerState) -> dict:
        await notify(state, "dod_audit_node", "NODE_SPAWNED")
        tasks = state["dispatch"]
        reports = await asyncio.gather(*(auditor(task, task["_hop_count"]) for task in tasks))
        audits = dict(state.get("audits", {}))
        for task, report in zip(tasks, reports):
            key = task["milestone_id"]
            last = next(item for item in reversed(state["results"]) if item["milestone_id"] == key)
            if last["error"]:
                report = dict(report)
                report["status"] = "DOD_FAILED"
                report["remediation_brief"] = (
                    report.get("remediation_brief", "") + "\nWorker error: " + last["error"]
                )
            audits[key] = report
            await notify(state, "dod_audit_node", report["status"], {"milestone_id": key, "audit": report})
        return {"audits": audits}

    async def human_review_gate(state: DaggerState, runtime: Runtime[dict]) -> dict:
        decision = (runtime.context or {}).get("decision", True)
        approved = decision is True or (isinstance(decision, str) and decision.lower() in {"approve", "approved", "yes"})
        all_passed = all(state["audits"].get(m["milestone_id"], {}).get("status") == "DOD_PASSED"
                         for m in state["milestones"])
        status = "Done" if approved and all_passed else "Failed"
        await notify(state, "human_review_gate", "COMPLETED" if status == "Done" else "FAILED",
                     {"approved": approved, "status": status})
        return {"status": status, "decision": decision}

    async def final_audit_node(state: DaggerState) -> dict:
        tasks = [{**task, "_kanban_id": state["kanban_id"], "_run_id": state["run_id"]}
                 for task in state["milestones"]]
        reports = await asyncio.gather(*(auditor(task, state["attempts"][task["milestone_id"]] - 1)
                                         for task in tasks))
        audits = {task["milestone_id"]: report for task, report in zip(tasks, reports)}
        for key, report in audits.items():
            await notify(state, "dod_audit_node", report["status"],
                         {"milestone_id": key, "audit": report, "final": True})
        return {"audits": audits, "final_checked": True}

    builder = StateGraph(DaggerState, context_schema=dict)
    builder.add_node("initialize_node", initialize_node)
    builder.add_node("pm_plan_node", pm_plan_node)
    builder.add_node("schedule_node", schedule_node)
    builder.add_node("container_worker_node", container_worker_node)
    builder.add_node("dod_audit_node", dod_audit_node)
    builder.add_node("final_audit_node", final_audit_node)
    builder.add_node("human_review_gate", human_review_gate)
    builder.add_edge(START, "initialize_node")
    builder.add_edge("initialize_node", "pm_plan_node")
    builder.add_edge("pm_plan_node", "schedule_node")
    builder.add_conditional_edges("schedule_node", fanout, ["container_worker_node", "final_audit_node", "human_review_gate"])
    builder.add_edge("container_worker_node", "dod_audit_node")
    builder.add_edge("dod_audit_node", "schedule_node")
    builder.add_edge("final_audit_node", "schedule_node")
    builder.add_edge("human_review_gate", END)
    return builder.compile(checkpointer=checkpointer if checkpointer is not None else InMemorySaver(),
                           interrupt_before=["human_review_gate"])


_studio_instance = None


async def _studio():
    global _studio_instance
    if _studio_instance is None:
        from .agents.orchestrator import Orchestrator
        from .agents.project_manager import ProjectManager
        from .db import DatabaseManager
        from .providers import ProviderRegistry
        from .schema import DefinitionOfDone, MilestoneTask, TaskEnvelope
        from uuid import uuid4

        db, registry = DatabaseManager(), ProviderRegistry()
        async def executor(prompt: str) -> str:
            milestone = MilestoneTask(title="Plan", dod={"acceptance_criteria": []})
            container = await _studio_instance.select(milestone)
            task = TaskEnvelope(task_id=str(uuid4()), run_id="studio", kanban_id="studio",
                                container_id=container.container_id, prompt=prompt,
                                dod=DefinitionOfDone(acceptance_criteria=[]))
            return await registry.get(container.provider).run(container, task)

        _studio_instance = Orchestrator(db, registry, ProjectManager(planner=executor))
    return _studio_instance


async def _studio_plan(state):
    from .schema import KanbanTask
    app = await _studio()
    if not await app.db.get_task(state["kanban_id"]):
        await app.db.upsert_task(KanbanTask(task_id=state["kanban_id"], title=state["goal"],
                                            description=state["goal"], run_id=state["run_id"]))
    return await app._plan(state)


async def _studio_work(state):
    return await (await _studio())._work(state)


async def _studio_audit(task, hop):
    return await (await _studio())._audit(task, hop)


async def _studio_emit(run_id, kanban_id, event, metadata):
    from .schema import KanbanTask
    app = await _studio()
    if not await app.db.get_task(kanban_id):
        await app.db.upsert_task(KanbanTask(task_id=kanban_id, title=kanban_id, run_id=run_id))
    await app._emit(run_id, kanban_id, event, metadata)


# Studio uses the same real application components; server supplies its own durable saver.
graph = build_graph(_studio_plan, _studio_work, _studio_audit, emit=_studio_emit)
