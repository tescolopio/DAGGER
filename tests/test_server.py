import shutil
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from typer.testing import CliRunner

from dagger import cli, server


@pytest.fixture
def client(monkeypatch):
    directory = Path(".dagger-server-tests") / uuid4().hex
    directory.mkdir(parents=True)

    class Generator:
        started = False
        stopped = False

        def __init__(self, db, **kwargs):
            self.db = db
            self.export_dir = kwargs["export_dir"]

        async def refresh_catalog(self):
            return await self.db.list_capabilities()

        def start(self):
            self.started = True

        async def aclose(self):
            self.stopped = True

    class Orchestrator:
        def __init__(self, db, registry, manager, *, bus, checkpointer, bus_path, worktree_path, skill_roots):
            self.bus = bus
            self.worktree_path = worktree_path
            self.calls = []

        async def start(self, run_id, goal, kanban_id=None):
            self.calls.append(("start", run_id, goal, kanban_id))
            await self.bus.set_graph({"nodes": [{"id": run_id}], "edges": []})
            await self.bus.publish({"event_type": "NODE_SPAWNED", "run_id": run_id})

        async def resume(self, run_id, decision):
            self.calls.append(("resume", run_id, decision))

        async def get_state(self, run_id):
            return {"run_id": run_id, "next": ["human_review_gate"]}

        async def get_graph(self, run_id=None):
            return await self.bus.get_graph()

    monkeypatch.setattr(server, "Orchestrator", Orchestrator)
    app = server.create_app(
        directory / "board.sqlite", generator_factory=Generator,
        container_dir=directory / "containers",
    )
    try:
        with TestClient(app) as http:
            yield http
        assert app.state.generator.started
        assert app.state.generator.stopped
    finally:
        shutil.rmtree(directory, ignore_errors=True)
        directory.parent.rmdir() if directory.parent.exists() and not any(directory.parent.iterdir()) else None


def test_health_catalog_and_task_validation(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/containers").status_code == 200
    assert client.get("/api/capabilities").status_code == 200
    assert client.post("/api/generator/refresh").status_code == 200
    response = client.post("/api/tasks", json={"title": "Build"})
    assert response.status_code == 201
    task = response.json()
    assert task["status"] == "Backlog"
    assert client.get("/api/tasks").json()[0]["task_id"] == task["task_id"]
    assert client.patch(f"/api/tasks/{task['task_id']}", json={"status": "bogus"}).status_code == 422
    assert client.patch("/api/tasks/missing", json={"status": "Running"}).status_code == 404


def test_graph_websocket_and_running_transition(client):
    task_id = client.post("/api/tasks", json={"title": "Ship it"}).json()["task_id"]
    with client.websocket_connect("/ws") as websocket:
        assert websocket.receive_json() == {"nodes": [], "edges": []}
        task = client.patch(f"/api/tasks/{task_id}", json={"status": "Running"}).json()
        assert task["run_id"]
        snapshots = [websocket.receive_json(), websocket.receive_json()]
        assert any(snapshot["nodes"] for snapshot in snapshots)
    assert client.get(f"/api/runs/{task['run_id']}/events").json() == []
    assert client.post(f"/api/tasks/{task_id}/approve").status_code == 202


def test_auto_start_and_missing_review(client):
    task = client.post("/api/tasks", json={"title": "Goal", "auto_start": True}).json()
    assert task["status"] == "Running"
    assert task["run_id"]
    assert client.post("/api/tasks/missing/approve").status_code == 404


def test_completion_requires_approval_and_restart_gets_new_run(client):
    assert client.post("/api/tasks", json={"title": "Bypass", "status": "Done"}).status_code == 409
    task = client.post("/api/tasks", json={"title": "Restart", "auto_start": True}).json()
    task_id = task["task_id"]
    assert client.post("/api/tasks", json={"task_id": task_id, "title": "Overwrite"}).status_code == 409
    assert client.patch(f"/api/tasks/{task_id}", json={"status": "Done"}).status_code == 409
    assert client.patch(f"/api/tasks/{task_id}", json={"run_id": "other"}).status_code == 422
    assert client.patch(f"/api/tasks/{task_id}", json={"status": "Failed"}).status_code == 200
    restarted = client.patch(f"/api/tasks/{task_id}", json={"status": "Running"})
    assert restarted.status_code == 200
    assert restarted.json()["run_id"] != task["run_id"]


def test_websocket_streams_graph_events(client):
    with client.websocket_connect("/ws/graph") as websocket:
        task = client.post("/api/tasks", json={"title": "Stream", "auto_start": True}).json()
        event = websocket.receive_json()
        assert event["event_type"] == "NODE_SPAWNED"
        assert event["run_id"] == task["run_id"]


def test_websocket_origin_policy(client):
    with pytest.raises(WebSocketDisconnect) as error:
        with client.websocket_connect("/ws/graph", headers={"origin": "https://evil.example"}):
            pass
    assert error.value.code == 1008
    with client.websocket_connect("/ws", headers={"origin": "http://localhost:5173"}) as websocket:
        assert websocket.receive_json() == {"nodes": [], "edges": []}


def test_container_yaml_hot_reload(client):
    directory = client.app.state.generator.export_dir
    time.sleep(0.2)
    (directory / "custom.yaml").write_text(
        "container_id: custom\nname: Custom\nrole: developer\nprovider: api\n",
        encoding="utf-8",
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if any(c["container_id"] == "custom" for c in client.get("/api/containers").json()):
            break
        time.sleep(0.1)
    else:
        pytest.fail("container definition was not reloaded")


def test_cli_default_and_commands(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    result = CliRunner().invoke(cli.app, ["serve"])
    assert result.exit_code == 0
    assert calls[0][1]["host"] == "127.0.0.1"
    assert calls[0][1]["port"] == 8420
    async def run(goal):
        calls.append(goal)

    async def refresh():
        calls.append("refreshed")

    monkeypatch.setattr(cli, "_run", run)
    monkeypatch.setattr(cli, "_refresh", refresh)
    assert CliRunner().invoke(cli.app, ["run", "Make a thing"]).exit_code == 0
    assert calls[-1] == "Make a thing"
    assert CliRunner().invoke(cli.app, ["refresh"]).exit_code == 0
    assert calls[-1] == "refreshed"
