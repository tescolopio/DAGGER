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


class EventBus:
    """Fan out independent snapshots to all current subscribers."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._graph: dict[str, list] = {"nodes": [], "edges": []}

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

    async def publish(self, event: dict[str, Any]) -> None:
        async with self._lock:
            self._broadcast(event)

    async def set_graph(self, graph: dict[str, list]) -> None:
        async with self._lock:
            self._graph = copy.deepcopy(graph)
            self._broadcast(self._graph)

    async def update_node(self, node: dict[str, Any]) -> None:
        async with self._lock:
            nodes = self._graph["nodes"]
            for index, existing in enumerate(nodes):
                if existing["id"] == node["id"]:
                    nodes[index] = copy.deepcopy(node)
                    break
            else:
                nodes.append(copy.deepcopy(node))
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
