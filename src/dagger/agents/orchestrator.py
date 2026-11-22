"""Connect the checkpointed graph to planning, providers and the local board."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ..bus import EventBus, FileBus
from ..graph import build_graph
from ..providers import ProviderRateLimitError
from ..schema import GraphEvent, KanbanTask, MilestoneTask, TaskEnvelope


class Orchestrator:
    def __init__(
        self, db, registry, project_manager, bus: EventBus | None = None,
        checkpointer=None, max_remediations: int = 2, *, max_hops: int | None = None,
        worktree_path: str | Path = ".", bus_path: str | Path = ".dagger/bus",
    ) -> None:
        self.db = db
        self.registry = registry
        self.project_manager = project_manager
        self.bus = bus or EventBus()
        self.max_hops = max_hops if max_hops is not None else max_remediations + 1
        if self.max_hops < 0:
            raise ValueError("max_hops must be non-negative")
        self.worktree = Path(worktree_path).resolve()
        self.inbox = FileBus(Path(bus_path) / "inbox")
        self.tmp = FileBus(Path(bus_path) / "tmp")
        self.outbox = FileBus(Path(bus_path) / "outbox")
        self.archive = FileBus(Path(bus_path) / "archive")
        self._locks: dict[str, asyncio.Lock] = {}
        self._board_locks: dict[str, asyncio.Lock] = {}
        self._container_limits: dict[str, asyncio.Semaphore] = {}
        self.graph = build_graph(self._plan, self._work, self._audit, emit=self._emit,
                                 checkpointer=checkpointer, max_hops=self.max_hops)

    @staticmethod
    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 1000}

    async def _update_task(self, kanban_id: str, **fields: Any) -> None:
        async with self._board_locks.setdefault(kanban_id, asyncio.Lock()):
            task = await self.db.get_task(kanban_id)
            if task:
                fields["updated_at"] = datetime.now(timezone.utc)
                await self.db.upsert_task(task.model_copy(update=fields))

    async def _update_milestone(self, kanban_id: str, milestone_id: str, **fields: Any) -> None:
        async with self._board_locks.setdefault(kanban_id, asyncio.Lock()):
            task = await self.db.get_task(kanban_id)
            if task:
                milestones = [m.model_copy(update=fields) if m.milestone_id == milestone_id else m
                              for m in task.milestones]
                await self.db.upsert_task(task.model_copy(
                    update={"milestones": milestones, "updated_at": datetime.now(timezone.utc)}))

    async def _emit(self, run_id: str, kanban_id: str, event_type: str, metadata: dict) -> None:
        node = metadata.get("node", "orchestrator")
        milestone_id = metadata.get("milestone_id")
        container_id = metadata.get("container_id")
        target = (f"agent:{container_id}" if node == "container_worker_node" and container_id
                  else f"worker:{milestone_id}" if node == "container_worker_node" and milestone_id
                  else node)
        source = {"container_worker_node": "pm_plan_node",
                  "dod_audit_node": f"worker:{milestone_id}" if milestone_id else "container_worker_node",
                  "human_review_gate": "dod_audit_node"}.get(node)
        source = metadata.get("source_node", source)
        if node == "pm_plan_node":
            source = "START"
        if event_type == "FALLBACK":
            source = f"agent:{metadata['previous_container_id']}"
        event = GraphEvent(run_id=run_id, kanban_id=kanban_id, event_type=event_type,
                           source_node=source, target_node=target, provider=metadata.get("provider", "system"),
                           container_id=metadata.get("container_id"),
                           label=event_type, metadata=metadata)
        await self.db.upsert_event(event)
        if milestone_id and event_type in {"DOD_PASSED", "DOD_FAILED"}:
            await self._update_milestone(kanban_id, milestone_id,
                                         status="passed" if event_type == "DOD_PASSED" else "failed")
        await self.bus.publish(event.model_dump(mode="json"))
        await self.bus.update_node({
            "id": f"{run_id}:{target}", "data": {"label": target, "status": event_type,
                                                  "run_id": run_id},
        })
        if source:
            src, dst = f"{run_id}:{source}", f"{run_id}:{target}"
            await self.bus.update_edge({"id": f"{src}->{dst}", "source": src, "target": dst})

    async def _plan(self, state: dict) -> dict:
        task = await self.db.get_task(state["kanban_id"])
        if task is None:
            raise ValueError(f"unknown kanban task: {state['kanban_id']}")
        await self._update_task(task.task_id, status="Planning", run_id=state["run_id"])
        plan = await self.project_manager.decompose_goal(task)
        await self._update_task(task.task_id, status="Running", milestones=list(plan.milestones))
        return {"milestones": [m.model_dump(mode="json") for m in plan.milestones],
                "dependencies": {key: list(value) for key, value in plan.dependencies.items()}}

    async def select(self, milestone: MilestoneTask):
        containers = [container for container in await self.db.list_containers() if container.active]
        capabilities = await self.db.list_capabilities()
        if capabilities:
            containers = [c for c in containers if not (
                matches := [cap for cap in capabilities if cap.provider == c.provider
                            and (not c.model or cap.model_name == c.model)]
            ) or any(cap.healthy for cap in matches)]
        if milestone.assigned_container_id:
            containers = [c for c in containers if c.container_id == milestone.assigned_container_id]
        if not containers:
            raise RuntimeError("no active matching agent container")
        skills = {skill.casefold() for skill in milestone.required_skills}
        priority = {"flat_subscription": 0, "local_free": 1, "metered_api": 2}
        return min(containers, key=lambda c: (-len(skills & {skill.casefold() for skill in c.strengths}),
                    priority.get(c.cost_tier, 3), c.container_id))

    async def _work(self, state: dict) -> str:
        raw = state["task"]
        milestone = MilestoneTask.model_validate({key: value for key, value in raw.items() if not key.startswith("_")})
        container = await self.select(milestone)
        await self._emit(state["run_id"], state["kanban_id"], "NODE_SPAWNED",
                         {"node": "container_worker_node", "milestone_id": milestone.milestone_id,
                          "container_id": container.container_id, "provider": container.provider})
        await self._emit(state["run_id"], state["kanban_id"], "EDGE_TRAVERSED",
                         {"node": "container_worker_node", "milestone_id": milestone.milestone_id,
                          "source_node": "orchestrator", "container_id": container.container_id,
                          "provider": container.provider})
        await self._update_milestone(state["kanban_id"], milestone.milestone_id,
                                     status="running", assigned_container_id=container.container_id)
        prompt = json.dumps({"title": milestone.title, "description": milestone.description,
                             "definition_of_done": milestone.dod.model_dump(mode="json"),
                             "remediation": raw.get("_remediation", "")}, ensure_ascii=False)
        envelope_id = str(uuid5(NAMESPACE_URL, json.dumps([state["run_id"], milestone.milestone_id])))
        envelope = TaskEnvelope(task_id=envelope_id, run_id=state["run_id"],
                                kanban_id=state["kanban_id"], container_id=container.container_id,
                                parent_task_id=milestone.milestone_id,
                                prompt=prompt, dod=milestone.dod, worktree_path=str(self.worktree),
                                hop_count=raw["_hop_count"], max_hops=self.max_hops)
        name = envelope.task_id + ".json"
        await asyncio.to_thread(self.inbox.write, name, envelope.model_dump(mode="json"))
        try:
            output = await self._run_provider(container, envelope)
        except Exception as exc:
            await asyncio.to_thread(self._publish_result, name, {"task_id": envelope.task_id, "error": str(exc)})
            await self._update_milestone(state["kanban_id"], milestone.milestone_id, status="failed")
            raise
        await asyncio.to_thread(self._publish_result, name, {"task_id": envelope.task_id, "result": output})
        await asyncio.to_thread(os.replace, self.inbox._path(name), self.archive._path(name))
        await self._update_milestone(state["kanban_id"], milestone.milestone_id,
                                     status="completed", output_summary=output)
        return output

    def _publish_result(self, name: str, message: dict) -> None:
        self.tmp.write(name, message)
        os.replace(self.tmp._path(name), self.outbox._path(name))

    async def _run_provider(self, container, envelope) -> str:
        candidates = [container]
        available = await self.db.list_containers()
        capabilities = await self.db.list_capabilities()
        for name in container.fallback_providers:
            # A fallback uses its own explicit credentials, model and cost tier.
            candidates.extend(c for c in available if c.active and c.provider == name
                              and c.container_id != container.container_id
                              and (not (matches := [cap for cap in capabilities if cap.provider == c.provider
                                                    and (not c.model or cap.model_name == c.model)])
                                   or any(cap.healthy for cap in matches)))
        last_error = None
        for index, candidate in enumerate(candidates):
            if index:
                await self._emit(envelope.run_id, envelope.kanban_id, "FALLBACK",
                                 {"node": "container_worker_node", "provider": candidate.provider,
                                  "container_id": candidate.container_id, "milestone_id": envelope.parent_task_id,
                                  "previous_container_id": candidates[index - 1].container_id})
                await self._update_milestone(envelope.kanban_id, envelope.parent_task_id,
                                             assigned_container_id=candidate.container_id)
            candidate_envelope = envelope.model_copy(update={"container_id": candidate.container_id})
            try:
                semaphore = self._container_limits.setdefault(candidate.container_id, asyncio.Semaphore(candidate.max_concurrency))
                async with semaphore:
                    return await self.registry.get(candidate.provider).run(candidate, candidate_envelope)
            except ProviderRateLimitError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _audit(self, task: dict, hop_count: int) -> dict:
        milestone = MilestoneTask.model_validate({key: value for key, value in task.items() if not key.startswith("_")})
        result = await self.project_manager.audit_milestone(milestone, self.worktree, hop_count + 1, self.max_hops)
        await self._update_milestone(task["_kanban_id"], milestone.milestone_id,
                                     status="passed" if result.status == "DOD_PASSED" else "failed")
        return asdict(result) if is_dataclass(result) else result.model_dump(mode="json")

    async def start(self, run_id: str, goal: str, kanban_id: str | None = None) -> dict:
        if not run_id or not goal:
            raise ValueError("run_id and goal are required")
        kanban_id = kanban_id or run_id
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            config = self._config(run_id)
            existing = await self.graph.aget_state(config)
            if existing.values:
                raise ValueError(f"run already exists: {run_id}")
            task = await self.db.get_task(kanban_id)
            if task is None:
                task = KanbanTask(task_id=kanban_id, title=goal, description=goal, run_id=run_id)
                await self.db.upsert_task(task)
            try:
                await self.graph.ainvoke({"run_id": run_id, "kanban_id": kanban_id, "goal": goal}, config)
                state = await self.get_state(run_id)
                if "human_review_gate" in state["next"]:
                    await self._update_task(kanban_id, status="Review")
                    await self._emit(run_id, kanban_id, "HUMAN_GATE", {"node": "human_review_gate"})
                return state
            except Exception:
                await self._update_task(kanban_id, status="Failed")
                raise

    async def resume(self, run_id: str, decision: Any = True) -> dict:
        async with self._locks.setdefault(run_id, asyncio.Lock()):
            config = self._config(run_id)
            current = await self.graph.aget_state(config)
            if "human_review_gate" not in current.next:
                raise ValueError(f"run is not awaiting review: {run_id}")
            self._validate_checkpoint(run_id, current.values)
            await self.graph.ainvoke(None, config, context={"decision": decision})
            state = await self.get_state(run_id)
            await self._update_task(state["kanban_id"], status=state["status"])
            return state

    @staticmethod
    def _validate_checkpoint(run_id: str, state: dict) -> None:
        if (not isinstance(state, dict) or state.get("run_id") != run_id
                or not isinstance(state.get("kanban_id"), str)
                or not state["kanban_id"] or not isinstance(state.get("milestones"), list)
                or not state["milestones"] or not isinstance(state.get("audits"), dict)):
            raise ValueError("invalid checkpoint state")
        for raw in state["milestones"]:
            milestone = MilestoneTask.model_validate(raw)
            audit = state["audits"].get(milestone.milestone_id)
            if audit is not None and (not isinstance(audit, dict)
                                      or audit.get("status") not in {"DOD_PASSED", "DOD_FAILED"}):
                raise ValueError("invalid checkpoint audit")

    async def get_state(self, run_id: str) -> dict:
        state = await self.graph.aget_state(self._config(run_id))
        return {**state.values, "next": list(state.next)}

    async def get_graph(self, run_id: str | None = None) -> dict:
        graph = await self.bus.get_graph()
        if run_id is None:
            return graph
        prefix = f"{run_id}:"
        nodes = {node["id"]: node for node in graph["nodes"] if node["id"].startswith(prefix)}
        edges = {edge["id"]: edge for edge in graph["edges"] if edge["source"].startswith(prefix)}
        for event in await self.db.list_events(run_id=run_id):
            target = prefix + event.target_node
            nodes[target] = {"id": target, "data": {"label": event.target_node,
                                                      "status": event.event_type, "run_id": run_id}}
            if event.source_node:
                source = prefix + event.source_node
                nodes.setdefault(source, {"id": source, "data": {"label": event.source_node, "run_id": run_id}})
                edge_id = f"{source}->{target}"
                edges[edge_id] = {"id": edge_id, "source": source, "target": target}
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}
