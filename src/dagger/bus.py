"""Atomic local handoffs and an asynchronous graph event bus."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any


_CONTROL_NODES = frozenset({
    "initialize",
    "initialize_node",
    "human_review_gate",
    "finalize",
    "schedule_node",
})

_AI_NODE_PREFIXES = ("worker:", "agent:")
_AI_NODES = frozenset({
    "pm_plan_node",
    "orchestrator",
    "dod_audit_node",
    "container_worker_node",
    "final_audit_node",
})


class EventBus:
    """Fan out independent snapshots to all current subscribers."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._graph: dict[str, list] = {"nodes": [], "edges": []}
        self._node_metadata: dict[str, dict[str, Any]] = {}

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        async with self._lock:
            self._subscribers.add(queue)
        return queue

    async def unsubscribe(self, queue: asyncio.Queue) -> None:
        async with self._lock:
            self._subscribers.discard(queue)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for queue in self._subscribers:
            queue.put_nowait(copy.deepcopy(event))

    @staticmethod
    def _is_control_node(label: str) -> bool:
        return label in _CONTROL_NODES

    @staticmethod
    def _is_ai_node(label: str) -> bool:
        return label in _AI_NODES or label.startswith(_AI_NODE_PREFIXES)

    def _record_event_metadata(self, event: dict[str, Any]) -> None:
        target = event.get("target_node")
        if not target or self._is_control_node(target):
            return
        meta = event.get("metadata") or {}
        provider = event.get("provider") or meta.get("provider")
        model = meta.get("model")
        skills = meta.get("skills") if isinstance(meta.get("skills"), list) and meta.get("skills") else None
        allowed_mcp_tools = (
            meta.get("allowed_mcp_tools")
            if isinstance(meta.get("allowed_mcp_tools"), list) and meta.get("allowed_mcp_tools")
            else None
        )
        has_provider = bool(provider and provider != "system")
        if not has_provider and not model and not skills and not allowed_mcp_tools:
            return
        run_id = event.get("run_id")
        keys = [target]
        if run_id:
            keys.append(f"{run_id}:{target}")
        milestone_id = meta.get("milestone_id")
        if milestone_id and (
            meta.get("node") == "container_worker_node"
            or target.startswith(("worker:", "agent:"))
        ):
            keys.append(f"worker:{milestone_id}")
            if run_id:
                keys.append(f"{run_id}:worker:{milestone_id}")
        for key in keys:
            entry = self._node_metadata.setdefault(key, {})
            if has_provider:
                entry["provider"] = provider
            if model:
                entry["model"] = model
            if skills:
                entry["skills"] = list(skills)
            if allowed_mcp_tools:
                entry["allowed_mcp_tools"] = list(allowed_mcp_tools)

    def _enrich_node(self, node: dict[str, Any]) -> dict[str, Any]:
        enriched = copy.deepcopy(node)
        raw_data = enriched.get("data")
        node_id = enriched.get("id", "")
        label = (raw_data.get("label") if isinstance(raw_data, dict) else None) or (
            node_id.split(":", 1)[-1] if ":" in node_id else node_id
        )

        if self._is_control_node(label):
            data = enriched.setdefault("data", {})
            data["provider"] = "system"
            return enriched

        if self._is_ai_node(label):
            data = enriched.setdefault("data", {})
            cached = {
                **(self._node_metadata.get(label) or {}),
                **(self._node_metadata.get(node_id) or {}),
            }
            provider = data.get("provider")
            if not provider or provider == "system":
                provider = cached.get("provider")
            if provider:
                data["provider"] = provider
            model = data.get("model") or cached.get("model")
            if model:
                data["model"] = model
            if not data.get("skills") and cached.get("skills"):
                data["skills"] = list(cached["skills"])
            if not data.get("allowed_mcp_tools") and cached.get("allowed_mcp_tools"):
                data["allowed_mcp_tools"] = list(cached["allowed_mcp_tools"])
            has_provider = bool(data.get("provider") and data["provider"] != "system")
            has_skills = bool(isinstance(data.get("skills"), list) and data["skills"])
            has_tools = bool(isinstance(data.get("allowed_mcp_tools"), list) and data["allowed_mcp_tools"])
            if has_provider or data.get("model") or has_skills or has_tools:
                for key in ([label, node_id] if node_id else [label]):
                    entry = self._node_metadata.setdefault(key, {})
                    if has_provider:
                        entry["provider"] = data["provider"]
                    if data.get("model"):
                        entry["model"] = data["model"]
                    if has_skills:
                        entry["skills"] = list(data["skills"])
                    if has_tools:
                        entry["allowed_mcp_tools"] = list(data["allowed_mcp_tools"])
        return enriched

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._lock:
            if event.get("event_type") != "CARD_LOG":
                self._record_event_metadata(event)
            self._broadcast(event)

    async def publish_card_log(self, entry: dict[str, Any]) -> None:
        payload = dict(entry)
        payload.setdefault("event_type", "CARD_LOG")
        async with self._lock:
            self._broadcast(payload)

    async def set_graph(self, graph: dict[str, list]) -> None:
        async with self._lock:
            enriched = copy.deepcopy(graph)
            enriched["nodes"] = [self._enrich_node(n) for n in enriched.get("nodes", [])]
            self._graph = enriched
            self._broadcast(self._graph)

    async def update_node(self, node: dict[str, Any]) -> None:
        async with self._lock:
            enriched = self._enrich_node(node)
            nodes = self._graph["nodes"]
            for index, existing in enumerate(nodes):
                if existing["id"] == enriched["id"]:
                    nodes[index] = enriched
                    break
            else:
                nodes.append(enriched)
            self._broadcast(self._graph)

    async def update_edge(self, edge: dict[str, Any]) -> None:
        async with self._lock:
            edges = self._graph["edges"]
            if not any(existing["id"] == edge["id"] for existing in edges):
                edges.append(copy.deepcopy(edge))
            self._broadcast(self._graph)

    async def get_graph(self) -> dict[str, list]:
        async with self._lock:
            return copy.deepcopy(self._graph)


class FileBus:
    """Publish complete JSON messages using atomic same-directory replacement."""

    def __init__(self, directory: str | Path) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or name in {".", ".."}:
            raise ValueError("invalid message name")
        return self.directory / name

    def write(self, name: str, message: Any) -> Path:
        target = self._path(name)
        data = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
        staging = self.directory / f".handoff-{uuid.uuid4().hex}"
        try:
            fd = os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staging, target)
        finally:
            staging.unlink(missing_ok=True)
        return target

    def read(self, name: str) -> Any:
        path = self._path(name)
        # Refuse to follow a substituted final-component symlink.
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            return json.load(stream)


MessageBus = FileBus
