"""Connect the checkpointed graph to planning, providers and the local board."""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from ..bus import EventBus, FileBus
from ..graph import build_graph
from ..providers import ProviderRateLimitError
from ..schema import GraphEvent, KanbanTask, MilestoneTask, TaskEnvelope

_WRITE_TOOLS = frozenset({"write", "edit", "patch", "shell", "bash"})
_IGNORED_CONTEXT_DIRS = frozenset({
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".dagger", "dist", "build",
    "data", ".tox", ".eggs",
})
_IGNORED_CONTEXT_FILES = frozenset({
    "uv.lock", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "poetry.lock", "Cargo.lock", "composer.lock", "Gemfile.lock",
})
_IGNORED_CONTEXT_SUFFIXES = frozenset({
    ".lock", ".pyc", ".pyo", ".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm",
})
_MAX_CONTEXT_FILES = 64


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
        self._last_executed_containers: dict[str, Any] = {}
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

    async def _emit_card_log(
        self,
        *,
        kanban_id: str,
        run_id: str = "",
        milestone_id: str | None = None,
        node_id: str = "orchestrator",
        provider: str = "system",
        channel: str = "system",
        text: str = "",
    ) -> None:
        if not kanban_id:
            return
        max_len = 4000
        bounded = text if len(text) <= max_len else text[: max_len - 3] + "..."
        entry = {
            "event_type": "CARD_LOG",
            "log_id": str(uuid4()),
            "kanban_id": kanban_id,
            "run_id": run_id or "",
            "milestone_id": milestone_id,
            "node_id": node_id,
            "provider": provider or "system",
            "channel": channel,
            "text": bounded,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if hasattr(self, "db") and hasattr(self.db, "append_card_log"):
            persisted = await self.db.append_card_log(entry)
            if isinstance(persisted, dict):
                entry = persisted
        if hasattr(self, "bus"):
            if hasattr(self.bus, "publish_card_log"):
                await self.bus.publish_card_log(entry)
            elif hasattr(self.bus, "publish"):
                await self.bus.publish(entry)

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
        provider = metadata.get("provider")
        model = metadata.get("model")
        if (not provider or provider == "system") and node in {
            "pm_plan_node", "dod_audit_node", "final_audit_node", "orchestrator", "container_worker_node"
        }:
            pm_prov = getattr(self.project_manager, "provider", None) if hasattr(self, "project_manager") else None
            pm_mod = getattr(self.project_manager, "model", None) if hasattr(self, "project_manager") else None
            if pm_prov and pm_prov != "system":
                provider = pm_prov
                model = model or pm_mod
            elif hasattr(self, "db"):
                active = [c for c in await self.db.list_containers() if c.active]
                if active:
                    provider = active[0].provider
                    model = model or active[0].model
        event_meta = dict(metadata)
        if model and not event_meta.get("model"):
            event_meta["model"] = model
        event = GraphEvent(run_id=run_id, kanban_id=kanban_id, event_type=event_type,
                           source_node=source, target_node=target, provider=provider or "system",
                           container_id=metadata.get("container_id"),
                           label=event_type, metadata=event_meta)
        await self.db.upsert_event(event)
        if milestone_id and event_type in {"DOD_PASSED", "DOD_FAILED"}:
            await self._update_milestone(kanban_id, milestone_id,
                                         status="passed" if event_type == "DOD_PASSED" else "failed")
        await self.bus.publish(event.model_dump(mode="json"))
        if event_type in {
            "FALLBACK",
            "DOD_PASSED",
            "DOD_FAILED",
            "HUMAN_GATE",
            "COMPLETED",
            "FAILED",
        }:
            await self._emit_card_log(
                kanban_id=kanban_id,
                run_id=run_id,
                milestone_id=milestone_id,
                node_id=target,
                provider=event.provider or "system",
                channel="system",
                text=f"{event_type} ({target})",
            )
        node_data: dict[str, Any] = {
            "label": target,
            "status": event_type,
            "run_id": run_id,
            "provider": event.provider,
        }
        if event_meta.get("model"):
            node_data["model"] = event_meta["model"]
        if isinstance(event_meta.get("skills"), list):
            node_data["skills"] = list(event_meta["skills"])
        if isinstance(event_meta.get("allowed_mcp_tools"), list):
            node_data["allowed_mcp_tools"] = list(event_meta["allowed_mcp_tools"])
        await self.bus.update_node({"id": f"{run_id}:{target}", "data": node_data})
        if node == "container_worker_node" and milestone_id and target != f"worker:{milestone_id}":
            worker_data = dict(node_data)
            worker_data["label"] = f"worker:{milestone_id}"
            await self.bus.update_node({"id": f"{run_id}:worker:{milestone_id}", "data": worker_data})
        if source:
            src, dst = f"{run_id}:{source}", f"{run_id}:{target}"
            await self.bus.update_edge({"id": f"{src}->{dst}", "source": src, "target": dst})

    async def _plan(self, state: dict) -> dict:
        task = await self.db.get_task(state["kanban_id"])
        if task is None:
            raise ValueError(f"unknown kanban task: {state['kanban_id']}")
        await self._update_task(task.task_id, status="Planning", run_id=state["run_id"])
        from .generator import AgentGenerator

        containers = await self.db.list_containers()
        entries = [AgentGenerator._rolodex_entry(c) for c in containers if c.active]
        entries.sort(key=lambda e: (e["cost_rank"], e["container_id"]))
        rolodex_text = AgentGenerator.generate_rolodex_text(entries, active_only=True)
        from ..skills import discover_skills, format_skills_for_planner

        skills_text = format_skills_for_planner(discover_skills(self.worktree))
        effective_run_id = state.get("run_id") or task.run_id or ""
        pm_provider = getattr(self.project_manager, "provider", "system") or "system"
        await self._emit_card_log(
            kanban_id=task.task_id,
            run_id=effective_run_id,
            node_id="pm_plan_node",
            provider=pm_provider,
            channel="system",
            text="PM planning started",
        )
        await self._emit_card_log(
            kanban_id=task.task_id,
            run_id=effective_run_id,
            node_id="pm_plan_node",
            provider=pm_provider,
            channel="dialogue",
            text=f"PM decompose_goal prompt: {task.description or task.title}",
        )
        from ..providers.base import card_log_hook

        async def _pm_stream_hook(channel: str, line: str) -> None:
            await self._emit_card_log(
                kanban_id=task.task_id,
                run_id=effective_run_id,
                node_id="pm_plan_node",
                provider=pm_provider,
                channel=channel,
                text=line,
            )

        token = card_log_hook.set(_pm_stream_hook)
        try:
            if rolodex_text or skills_text:
                try:
                    plan = await self.project_manager.decompose_goal(
                        task, rolodex_text=rolodex_text, skills_text=skills_text
                    )
                except TypeError:
                    try:
                        plan = await self.project_manager.decompose_goal(task, rolodex_text=rolodex_text)
                    except TypeError:
                        plan = await self.project_manager.decompose_goal(task)
            else:
                plan = await self.project_manager.decompose_goal(task)
        finally:
            card_log_hook.reset(token)
        await self._update_task(task.task_id, status="Running", milestones=list(plan.milestones))
        await self._emit_card_log(
            kanban_id=task.task_id,
            run_id=effective_run_id,
            node_id="pm_plan_node",
            provider=pm_provider,
            channel="dialogue",
            text=f"PM plan completed: {len(plan.milestones)} milestone(s)",
        )
        await self._emit_card_log(
            kanban_id=task.task_id,
            run_id=effective_run_id,
            node_id="pm_plan_node",
            provider=pm_provider,
            channel="system",
            text=f"PM planning completed ({len(plan.milestones)} milestone(s))",
        )
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
        lightweight_skills = {"review", "analysis", "triage"}
        heavy_skills = {"coding", "patching", "architecture", "testing"}
        is_lightweight = bool(skills & lightweight_skills) and not (skills & heavy_skills)

        def is_lightweight_container(c) -> bool:
            model_name = (c.model or "").casefold()
            return c.cost_tier == "local_free" or "flash" in model_name or "hydrogen" in model_name

        if is_lightweight:
            priority = {"local_free": 0, "flat_subscription": 1, "metered_api": 2}
            return min(containers, key=lambda c: (
                -len(skills & {skill.casefold() for skill in c.strengths}),
                0 if is_lightweight_container(c) else 1,
                priority.get(c.cost_tier, 3),
                c.container_id,
            ))

        priority = {"flat_subscription": 0, "local_free": 1, "metered_api": 2}
        return min(containers, key=lambda c: (
            -len(skills & {skill.casefold() for skill in c.strengths}),
            1 if is_lightweight_container(c) else 0,
            priority.get(c.cost_tier, 3),
            c.container_id,
        ))

    @staticmethod
    def _is_read_only_container(container) -> bool:
        tools = {tool.strip().casefold() for tool in container.allowed_tools if tool.strip()}
        return bool(tools) and tools.isdisjoint(_WRITE_TOOLS)

    @staticmethod
    def _target_paths(milestone: MilestoneTask) -> list[str]:
        targets: list[str] = []
        for path in (*milestone.allowed_write_paths, *milestone.dod.expected_artifacts):
            cleaned = path.strip()
            if cleaned and cleaned not in targets:
                targets.append(cleaned)
        return targets

    def _discover_context_files(self, milestone: MilestoneTask, raw: dict[str, Any]) -> list[str]:
        explicit = raw.get("_context_files")
        if isinstance(explicit, (list, tuple)):
            filtered: list[str] = []
            for item in explicit:
                if not isinstance(item, str):
                    continue
                cleaned = item.strip()
                if not cleaned:
                    continue
                name = Path(cleaned).name
                if name in _IGNORED_CONTEXT_FILES or Path(name).suffix.lower() in _IGNORED_CONTEXT_SUFFIXES:
                    continue
                if cleaned not in filtered:
                    filtered.append(cleaned)
            return filtered[:_MAX_CONTEXT_FILES]

        root = self.worktree.resolve()
        if not root.is_dir():
            return []

        discovered: list[str] = []
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            dirnames[:] = sorted(
                d for d in dirnames
                if d not in _IGNORED_CONTEXT_DIRS and not d.endswith(".egg-info")
            )
            current = Path(dirpath)
            for filename in sorted(filenames):
                if (filename in _IGNORED_CONTEXT_FILES
                        or filename.startswith(".dagger-")
                        or Path(filename).suffix.lower() in _IGNORED_CONTEXT_SUFFIXES):
                    continue
                rel = (current / filename).relative_to(root).as_posix()
                discovered.append(rel)

        hint_text = " ".join([
            milestone.title,
            milestone.description,
            *milestone.dod.acceptance_criteria,
            *milestone.dod.verification_commands,
            *milestone.dod.expected_artifacts,
            *milestone.allowed_write_paths,
        ])
        prioritized = [path for path in discovered if path in hint_text or Path(path).name in hint_text]
        remaining = [path for path in discovered if path not in prioritized]
        return (prioritized + remaining)[:_MAX_CONTEXT_FILES]

    @staticmethod
    def _single_materialization_target(milestone: MilestoneTask) -> str | None:
        expected = list(dict.fromkeys(
            path.strip() for path in milestone.dod.expected_artifacts if isinstance(path, str) and path.strip()
        ))
        allowed = list(dict.fromkeys(
            path.strip() for path in milestone.allowed_write_paths if isinstance(path, str) and path.strip()
        ))
        if expected and allowed:
            if len(expected) != 1:
                return None
            target = expected[0]
            if target not in allowed:
                raise ValueError(f"Target artifact {target!r} is not authorized in allowed_write_paths")
            return target
        candidates = expected or allowed
        if len(candidates) != 1:
            return None
        return candidates[0]

    def _resolve_worktree_target(self, raw_path: str) -> Path:
        if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
            raise ValueError(f"Invalid target artifact path: {raw_path!r}")
        cleaned = raw_path.strip()
        candidate = Path(cleaned)
        if ".." in candidate.parts:
            raise ValueError(f"Refusing path traversal outside worktree root: {raw_path!r}")
        root = self.worktree.resolve()
        combined = candidate if candidate.is_absolute() else (root / candidate)
        if combined.is_symlink():
            raise ValueError(f"Refusing symlink target artifact path: {raw_path!r}")
        try:
            resolved = combined.resolve()
            resolved_parent = resolved.parent.resolve()
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"Invalid target artifact path {raw_path!r}: {exc}") from exc
        if resolved == root or not resolved.is_relative_to(root) or not resolved_parent.is_relative_to(root):
            raise ValueError(f"Refusing target artifact path outside worktree root: {raw_path!r}")
        rel = resolved.relative_to(root)
        if rel.parts and rel.parts[0] == ".dagger":
            raise ValueError(f"Refusing target artifact path inside .dagger vault directory: {raw_path!r}")
        return resolved

    def _atomic_write_text(self, target: Path, text: str) -> Path:
        root = self.worktree.resolve()
        if target == root or not target.is_relative_to(root):
            raise ValueError(f"Refusing to write outside worktree root: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved_parent = target.parent.resolve()
        if not resolved_parent.is_relative_to(root):
            raise ValueError(f"Refusing to write outside worktree root: {target}")
        final_target = resolved_parent / target.name
        if final_target.is_symlink():
            raise ValueError(f"Refusing to overwrite symlink target: {final_target}")
        if final_target.exists():
            return final_target
        data = text.encode("utf-8")
        staging = resolved_parent / f".dagger-artifact-{uuid4().hex}"
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, final_target)
        finally:
            staging.unlink(missing_ok=True)
        return final_target

    def _materialize_read_only_output(
        self, container, milestone: MilestoneTask, output: str
    ) -> Path | None:
        for path in self._target_paths(milestone):
            self._resolve_worktree_target(path)
        if not self._is_read_only_container(container):
            return None
        target_raw = self._single_materialization_target(milestone)
        if target_raw is None:
            return None
        target = self._resolve_worktree_target(target_raw)
        if target.exists():
            return None
        if not isinstance(output, str) or not output.strip():
            return None
        return self._atomic_write_text(target, output.strip() + "\n")

    async def _work(self, state: dict) -> str:
        raw = state["task"]
        milestone = MilestoneTask.model_validate({key: value for key, value in raw.items() if not key.startswith("_")})
        container = await self.select(milestone)
        from ..skills import SkillRegistry, format_matched_skills_for_prompt

        read_only = self._is_read_only_container(container)
        skill_registry = SkillRegistry(self.worktree)
        matched_skills = skill_registry.match(
            required_skills=list(milestone.required_skills),
            context_text=f"{milestone.title} {milestone.description}",
            read_only=read_only,
        )
        skill_names = [s.name for s in matched_skills]
        allowed_mcp_tools = skill_registry.resolve_allowed_mcp_tools(
            matched_skills, read_only=read_only
        )

        await self._emit(state["run_id"], state["kanban_id"], "NODE_SPAWNED",
                         {"node": "orchestrator", "provider": container.provider, "model": container.model})
        await self._emit(state["run_id"], state["kanban_id"], "NODE_SPAWNED",
                         {"node": "container_worker_node", "milestone_id": milestone.milestone_id,
                          "container_id": container.container_id, "provider": container.provider,
                          "model": container.model, "skills": skill_names,
                          "allowed_mcp_tools": allowed_mcp_tools})
        await self._emit(state["run_id"], state["kanban_id"], "EDGE_TRAVERSED",
                         {"node": "container_worker_node", "milestone_id": milestone.milestone_id,
                          "source_node": "orchestrator", "container_id": container.container_id,
                          "provider": container.provider, "model": container.model,
                          "skills": skill_names, "allowed_mcp_tools": allowed_mcp_tools})
        await self._update_milestone(state["kanban_id"], milestone.milestone_id,
                                     status="running", assigned_container_id=container.container_id)
        context_files = await asyncio.to_thread(self._discover_context_files, milestone, raw)
        target_paths = self._target_paths(milestone)
        prompt_payload: dict[str, Any] = {
            "title": milestone.title,
            "description": milestone.description,
            "definition_of_done": milestone.dod.model_dump(mode="json"),
            "context_files": context_files,
            "target_paths": target_paths,
            "allowed_write_paths": list(milestone.allowed_write_paths),
            "instructions": (
                "Focus only on the listed context_files and target_paths; "
                "avoid scanning lockfiles or dependency directories."
            ),
            "remediation": raw.get("_remediation", ""),
        }
        if matched_skills:
            prompt_payload["matched_skills"] = skill_names
            prompt_payload["skill_guidance"] = format_matched_skills_for_prompt(
                matched_skills,
                read_only=read_only,
                allowed_mcp_tools=allowed_mcp_tools,
            )
        prompt = json.dumps(prompt_payload, ensure_ascii=False)
        envelope_id = str(uuid5(NAMESPACE_URL, json.dumps([state["run_id"], milestone.milestone_id])))
        envelope = TaskEnvelope(task_id=envelope_id, run_id=state["run_id"],
                                kanban_id=state["kanban_id"], container_id=container.container_id,
                                parent_task_id=milestone.milestone_id,
                                prompt=prompt, dod=milestone.dod, worktree_path=str(self.worktree),
                                allowed_write_paths=list(milestone.allowed_write_paths),
                                allowed_mcp_tools=allowed_mcp_tools,
                                hop_count=raw["_hop_count"], max_hops=self.max_hops)
        name = envelope.task_id + ".json"
        await asyncio.to_thread(self.inbox.write, name, envelope.model_dump(mode="json"))
        try:
            for path in target_paths:
                self._resolve_worktree_target(path)
            output = await self._run_provider(container, envelope)
            executed_container = getattr(self, "_last_executed_containers", {}).pop(envelope.task_id, container)
            await asyncio.to_thread(self._materialize_read_only_output, executed_container, milestone, output)
        except Exception as exc:
            getattr(self, "_last_executed_containers", {}).pop(envelope.task_id, None)
            await asyncio.to_thread(self._publish_result, name, {"task_id": envelope.task_id, "error": str(exc)})
            await self._update_milestone(state["kanban_id"], milestone.milestone_id, status="failed")
            raise
        await asyncio.to_thread(self._publish_result, name, {"task_id": envelope.task_id, "result": output})
        await asyncio.to_thread(os.replace, self.inbox._path(name), self.archive._path(name))
        await self._update_milestone(state["kanban_id"], milestone.milestone_id,
                                     status="completed", output_summary=output)
        return output

    def _snapshot_worktree(self) -> dict[str, tuple[int, int]]:
        root = Path(self.worktree).resolve()
        if not root.exists():
            return {}
        snap: dict[str, tuple[int, int]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _IGNORED_CONTEXT_DIRS and not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                try:
                    st = p.stat()
                    snap[p.relative_to(root).as_posix()] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    continue
        return snap

    def _publish_result(self, name: str, message: dict) -> None:
        self.tmp.write(name, message)
        os.replace(self.tmp._path(name), self.outbox._path(name))

    async def _run_provider(self, container, envelope) -> str:
        candidates = [container]
        available = await self.db.list_containers()
        capabilities = await self.db.list_capabilities()
        seen_ids = {container.container_id}
        for name in container.fallback_providers:
            # A fallback uses its own explicit credentials, model and cost tier.
            for c in available:
                if (
                    c.active
                    and (c.container_id == name or c.provider == name)
                    and c.container_id not in seen_ids
                    and (not (matches := [cap for cap in capabilities if cap.provider == c.provider
                                          and (not c.model or cap.model_name == c.model)])
                         or any(cap.healthy for cap in matches))
                ):
                    candidates.append(c)
                    seen_ids.add(c.container_id)
        for c in available:
            if c.active and c.container_id not in seen_ids:
                matches = [cap for cap in capabilities if cap.provider == c.provider
                           and (not c.model or cap.model_name == c.model)]
                if not matches or any(cap.healthy for cap in matches):
                    candidates.append(c)
                    seen_ids.add(c.container_id)
        last_error = None
        for index, candidate in enumerate(candidates):
            if index:
                await self._emit(envelope.run_id, envelope.kanban_id, "FALLBACK",
                                 {"node": "container_worker_node", "provider": candidate.provider,
                                  "model": candidate.model,
                                  "container_id": candidate.container_id, "milestone_id": envelope.parent_task_id,
                                  "previous_container_id": candidates[index - 1].container_id})
                await self._emit(envelope.run_id, envelope.kanban_id, "EDGE_TRAVERSED",
                                 {"node": "container_worker_node", "source_node": "container_worker_node",
                                  "provider": candidate.provider, "model": candidate.model,
                                  "container_id": candidate.container_id,
                                  "milestone_id": envelope.parent_task_id,
                                  "previous_container_id": candidates[index - 1].container_id,
                                  "fallback": True})
                await self._update_milestone(envelope.kanban_id, envelope.parent_task_id,
                                             assigned_container_id=candidate.container_id)
            candidate_envelope = envelope.model_copy(update={"container_id": candidate.container_id})
            try:
                semaphore = self._container_limits.setdefault(candidate.container_id, asyncio.Semaphore(candidate.max_concurrency))
                usage_context = {
                    "run_id": envelope.run_id,
                    "kanban_id": envelope.kanban_id,
                    "container_id": candidate.container_id,
                    "cost_tier": candidate.cost_tier,
                    "on_swarm_post": (
                        (lambda post, cand=candidate: self._emit(
                            envelope.run_id,
                            envelope.kanban_id,
                            "SWARM_POST",
                            {
                                "node": "container_worker_node",
                                "node_id": f"worker:{envelope.parent_task_id}",
                                "milestone_id": envelope.parent_task_id,
                                "container_id": cand.container_id,
                                "provider": cand.provider,
                                "model": cand.model,
                                **post,
                            },
                        ))
                        if candidate.provider == "agy" and (
                            candidate.container_id == "agy_swarm_squad" or candidate.role == "swarm_squad"
                        )
                        else None
                    ),
                }
                async with semaphore:
                    runner = self.registry.get(candidate.provider)
                    check_boundary = (
                        candidate.provider == "agy"
                        and (candidate.container_id == "agy_swarm_squad" or candidate.role == "swarm_squad")
                        and bool(getattr(candidate_envelope, "allowed_write_paths", None))
                    )
                    before_snap = self._snapshot_worktree() if check_boundary else None
                    from ..providers.base import card_log_hook

                    await self._emit_card_log(
                        kanban_id=envelope.kanban_id or envelope.task_id,
                        run_id=envelope.run_id or "",
                        milestone_id=envelope.parent_task_id or envelope.task_id,
                        node_id=f"agent:{candidate.container_id}",
                        provider=candidate.provider,
                        channel="dialogue",
                        text=f"Worker prompt ({candidate.container_id}): {candidate_envelope.prompt[:400]}",
                    )

                    async def _stream_hook(channel: str, line: str) -> None:
                        await self._emit_card_log(
                            kanban_id=envelope.kanban_id or envelope.task_id,
                            run_id=envelope.run_id or "",
                            milestone_id=envelope.parent_task_id or envelope.task_id,
                            node_id=f"agent:{candidate.container_id}",
                            provider=candidate.provider,
                            channel=channel,
                            text=line,
                        )

                    token = card_log_hook.set(_stream_hook)
                    try:
                        try:
                            result = await runner.run(
                                candidate, candidate_envelope, usage_context=usage_context
                            )
                        except TypeError:
                            result = await runner.run(candidate, candidate_envelope)
                    finally:
                        card_log_hook.reset(token)

                    await self._emit_card_log(
                        kanban_id=envelope.kanban_id or envelope.task_id,
                        run_id=envelope.run_id or "",
                        milestone_id=envelope.parent_task_id or envelope.task_id,
                        node_id=f"agent:{candidate.container_id}",
                        provider=candidate.provider,
                        channel="dialogue",
                        text=f"Worker response ({candidate.container_id}): {str(result)[:600]}",
                    )
                    if check_boundary and before_snap is not None:
                        after_snap = self._snapshot_worktree()
                        changed = [
                            p for p, mtime in after_snap.items()
                            if before_snap.get(p) != mtime
                        ]
                        allowed = tuple(p.rstrip("/") for p in candidate_envelope.allowed_write_paths)
                        violations = [
                            p for p in changed
                            if not any(p == a or p.startswith(a + "/") for a in allowed)
                        ]
                        if violations:
                            from ..providers.base import ProviderError
                            raise ProviderError(f"agy_swarm_squad write boundary violation: {violations}")
                    if hasattr(self, "_last_executed_containers"):
                        self._last_executed_containers[envelope.task_id] = candidate
                    return result
            except (ProviderRateLimitError, RuntimeError, ValueError, OSError, asyncio.TimeoutError) as exc:
                if type(exc).__name__ == "ProviderError":
                    msg = str(exc).lower()
                    if not any(m in msg for m in (
                        "status 41", "exit 41", "exit code 41", "could not start", "timed out",
                        "auth", "not found", "missing", "login", "unauthorized",
                        "stall", "stalled", "allowed_write_paths", "write boundary", "violation",
                    )):
                        raise
                last_error = exc
        assert last_error is not None
        raise last_error

    async def _audit(self, task: dict, hop_count: int) -> dict:
        milestone = MilestoneTask.model_validate({key: value for key, value in task.items() if not key.startswith("_")})
        result = await self.project_manager.audit_milestone(
            milestone, self.worktree, hop_count + 1, self.max_hops, run_id=task.get("_run_id")
        )
        kanban_id = task.get("_kanban_id", "")
        run_id = task.get("_run_id", "")
        pm_provider = getattr(self.project_manager, "provider", "system") or "system"
        for cmd_ev in getattr(result, "command_results", None) or getattr(result, "commands", ()) or ():
            await self._emit_card_log(
                kanban_id=kanban_id,
                run_id=run_id,
                milestone_id=milestone.milestone_id,
                node_id="dod_audit_node",
                provider="system",
                channel="command",
                text=f"$ {cmd_ev.command} -> exit={cmd_ev.returncode} ({'PASS' if cmd_ev.passed else 'FAIL'})",
            )
            if getattr(cmd_ev, "stdout", ""):
                await self._emit_card_log(
                    kanban_id=kanban_id,
                    run_id=run_id,
                    milestone_id=milestone.milestone_id,
                    node_id="dod_audit_node",
                    provider="system",
                    channel="stdout",
                    text=cmd_ev.stdout[:2000],
                )
            if getattr(cmd_ev, "stderr", ""):
                await self._emit_card_log(
                    kanban_id=kanban_id,
                    run_id=run_id,
                    milestone_id=milestone.milestone_id,
                    node_id="dod_audit_node",
                    provider="system",
                    channel="stderr",
                    text=cmd_ev.stderr[:2000],
                )
        await self._emit_card_log(
            kanban_id=kanban_id,
            run_id=run_id,
            milestone_id=milestone.milestone_id,
            node_id="dod_audit_node",
            provider=pm_provider,
            channel="dialogue",
            text=f"DoD audit {result.status}: {milestone.milestone_id}",
        )
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
            existing_data = nodes.get(target, {}).get("data", {})
            node_data: dict[str, Any] = {
                "label": event.target_node,
                "status": event.event_type,
                "run_id": run_id,
                "provider": event.provider,
            }
            if isinstance(event.metadata, dict) and event.metadata.get("model"):
                node_data["model"] = event.metadata["model"]
            elif existing_data.get("model"):
                node_data["model"] = existing_data["model"]
            meta_skills = event.metadata.get("skills") if isinstance(event.metadata, dict) else None
            if isinstance(meta_skills, list) and meta_skills:
                node_data["skills"] = list(meta_skills)
            elif existing_data.get("skills"):
                node_data["skills"] = list(existing_data["skills"])
            meta_tools = event.metadata.get("allowed_mcp_tools") if isinstance(event.metadata, dict) else None
            if isinstance(meta_tools, list) and meta_tools:
                node_data["allowed_mcp_tools"] = list(meta_tools)
            elif existing_data.get("allowed_mcp_tools"):
                node_data["allowed_mcp_tools"] = list(existing_data["allowed_mcp_tools"])
            nodes[target] = {"id": target, "data": node_data}
            if (
                isinstance(event.metadata, dict)
                and event.metadata.get("node") == "container_worker_node"
                and event.metadata.get("milestone_id")
                and (node_data.get("skills") or node_data.get("allowed_mcp_tools"))
            ):
                worker_id = f"{prefix}worker:{event.metadata['milestone_id']}"
                if worker_id in nodes:
                    w_data = nodes[worker_id].setdefault("data", {})
                    if node_data.get("skills"):
                        w_data["skills"] = list(node_data["skills"])
                    if node_data.get("allowed_mcp_tools"):
                        w_data["allowed_mcp_tools"] = list(node_data["allowed_mcp_tools"])
            if event.source_node:
                source = prefix + event.source_node
                nodes.setdefault(source, {"id": source, "data": {"label": event.source_node, "run_id": run_id}})
                edge_id = f"{source}->{target}"
                edges[edge_id] = {"id": edge_id, "source": source, "target": target}
        return {"nodes": list(nodes.values()), "edges": list(edges.values())}
