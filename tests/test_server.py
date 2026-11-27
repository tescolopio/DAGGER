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


def test_select_pm_containers_excludes_swarm_and_ranks_planners_deterministically():
    from dagger.schema import AgentContainer, AvailableModel, EndpointCapability

    containers = [
        AgentContainer(container_id="agy_swarm_squad", name="Swarm", role="swarm_squad", provider="agy", model="Gemini 4 Neon (High)"),
        AgentContainer(container_id="custom_swarm", name="Custom Swarm", role="swarm_squad", provider="agy", model="Gemini 4 Neon (High)"),
        AgentContainer(container_id="tavily_researcher", name="Tavily", role="researcher", provider="api", model="tavily-search", cost_tier="metered_api"),
        AgentContainer(container_id="audio_tool", name="Whisper Tool", role="researcher", provider="api", model="whisper-large-v3", cost_tier="metered_api"),
        AgentContainer(container_id="groq_fast_router", name="Groq", role="fast_router", provider="api", model="llama-3.3-70b-versatile", cost_tier="metered_api"),
        AgentContainer(container_id="fast_auditor_agy", name="Fast Agy", role="fast_auditor", provider="agy", model="Gemini 4 Hydrogen (Low)"),
        AgentContainer(container_id="copilot_patcher", name="Copilot", role="patcher", provider="copilot", model="gpt-4.1"),
        AgentContainer(container_id="repo_auditor_gemini", name="Repo Gemini", role="repo_auditor", provider="gemini", model="gemini-2.5-pro"),
        AgentContainer(container_id="repo_auditor_agy", name="Repo Agy", role="repo_auditor", provider="agy", model="Gemini 4 Neon (High)"),
        AgentContainer(container_id="architect_claude", name="Architect", role="architect", provider="claude", model="claude-sonnet-4-5"),
    ]
    capabilities = [
        EndpointCapability(
            provider=c.provider,
            model_name=c.model or "default",
            healthy=True,
            is_llm=(c.container_id not in {"tavily_researcher", "audio_tool"}),
        )
        for c in containers
    ]

    ranked = server._select_pm_containers(containers, capabilities)
    assert [c.container_id for c in ranked] == [
        "architect_claude",
        "repo_auditor_agy",
        "repo_auditor_gemini",
        "copilot_patcher",
        "fast_auditor_agy",
        "groq_fast_router",
    ]
    assert "tavily_researcher" not in [c.container_id for c in ranked]
    assert "audio_tool" not in [c.container_id for c in ranked]

    # When discovered models mark claude-sonnet-4-5 unavailable but gemini-2.5-pro available,
    # confirmed-available containers are preferred ahead of unverified/unavailable ones.
    discovered = [
        AvailableModel(provider_id="claude", model_id="claude-sonnet-4-5", available=False, is_llm=True),
        AvailableModel(provider_id="gemini", model_id="gemini-2.5-pro", available=True, is_llm=True),
    ]
    ranked_with_models = server._select_pm_containers(containers, capabilities, discovered)
    assert ranked_with_models[0].container_id != "architect_claude"
    assert ranked_with_models[-1].container_id == "architect_claude"

    # When only swarm containers are active/healthy, none are eligible for PM planning.
    swarm_only = [c for c in containers if c.role == "swarm_squad"]
    assert server._select_pm_containers(swarm_only, capabilities) == []


def test_pm_execute_skips_swarm_enforces_read_only_sets_worktree_and_falls_back(tmp_path, monkeypatch):
    import json
    from dagger.providers import BaseProvider, ProviderError, ProviderRegistry
    from dagger.schema import AgentContainer, EndpointCapability

    project_dir = tmp_path / "target_repo"
    project_dir.mkdir()
    (project_dir / "README.md").write_text("# CS Guru\n", encoding="utf-8")

    plan_calls: list[tuple[str, list[str], str | None]] = []

    class FakeProvider(BaseProvider):
        def __init__(self, name: str):
            super().__init__(name)

        async def run(self, container, task):
            if "Decompose this project" in task.prompt:
                plan_calls.append((container.container_id, list(container.allowed_tools), task.worktree_path))
                if container.container_id == "architect_claude":
                    raise ProviderError("claude primary unavailable")
                dod = {
                    "acceptance_criteria": ["README exists"],
                    "expected_artifacts": ["README.md"],
                    "verification_commands": ["test -s README.md"],
                }
                return "```json\n" + json.dumps({
                    "milestones": [
                        {"milestone_id": "m1", "title": "Assess", "required_skills": ["review"], "dod": dod},
                        {"milestone_id": "m2", "title": "Plan", "depends_on": ["m1"], "required_skills": ["review"], "dod": dod},
                    ]
                }) + "\n```"
            return "worker-ok"

    class Registry(ProviderRegistry):
        def __init__(self, **kwargs):
            super().__init__(defaults=False, **kwargs)
            for name in ("claude", "agy"):
                self.register(name, FakeProvider(name))

    class Generator:
        def __init__(self, db, **kwargs):
            self.db = db

        async def refresh_catalog(self):
            for c in await self.db.list_containers():
                await self.db.upsert_container(c.model_copy(update={"active": False}))
            seeded = [
                AgentContainer(
                    container_id="agy_swarm_squad", name="Swarm", role="swarm_squad",
                    provider="agy", model="Gemini 4 Neon (High)",
                    allowed_tools=["read", "search", "write", "edit"], active=True,
                ),
                AgentContainer(
                    container_id="architect_claude", name="Architect", role="architect",
                    provider="claude", model="claude-sonnet-4-5",
                    allowed_tools=["read", "search", "write"], active=True,
                ),
                AgentContainer(
                    container_id="repo_auditor_agy", name="Repo Agy", role="repo_auditor",
                    provider="agy", model="Gemini 4 Neon (High)",
                    allowed_tools=["read", "search", "edit"], strengths=["review"], active=True,
                ),
                AgentContainer(
                    container_id="fast_auditor_agy", name="Fast Agy", role="fast_auditor",
                    provider="agy", model="Gemini 4 Hydrogen (Low)",
                    allowed_tools=["read", "search"], strengths=["review"], active=True,
                ),
            ]
            for c in seeded:
                await self.db.upsert_container(c)
                await self.db.upsert_capability(
                    EndpointCapability(provider=c.provider, model_name=c.model or "default", healthy=True)
                )

        def start(self):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(server, "ProviderRegistry", Registry)
    app = server.create_app(
        tmp_path / "state" / "dagger.sqlite3",
        tmp_path / "state" / "checkpoints.sqlite3",
        generator_factory=Generator,
        container_dir=tmp_path / "containers",
        project=project_dir,
    )
    with TestClient(app) as http:
        resp = http.post("/api/tasks", json={"title": "Assess CS Guru", "auto_start": True})
        assert resp.status_code == 201
        task_id = resp.json()["task_id"]
        for _ in range(100):
            board = http.get("/api/tasks").json()
            if board[0]["status"] in {"Review", "Failed"}:
                break
            time.sleep(0.05)
        assert board[0]["status"] == "Review"

        # Swarm was never called; architect_claude failed over cleanly to repo_auditor_agy;
        # both PM invocations ran read-only with worktree_path == project_dir.
        expected_root = str(project_dir.resolve())
        assert plan_calls == [
            ("architect_claude", ["read", "search"], expected_root),
            ("repo_auditor_agy", ["read", "search"], expected_root),
        ]

        # Persisted container definitions were not mutated by the read-only PM copy.
        containers_by_id = {c["container_id"]: c for c in http.get("/api/containers").json()}
        assert containers_by_id["architect_claude"]["allowed_tools"] == ["read", "search", "write"]
        assert containers_by_id["repo_auditor_agy"]["allowed_tools"] == ["read", "search", "edit"]


def test_pm_execute_all_candidates_fail_surfaces_error_in_card_logs(tmp_path, monkeypatch):
    from dagger.providers import BaseProvider, ProviderError, ProviderRegistry
    from dagger.schema import AgentContainer, EndpointCapability

    project_dir = tmp_path / "repo"
    project_dir.mkdir()

    class FailingProvider(BaseProvider):
        def __init__(self, name: str):
            super().__init__(name)

        async def run(self, container, task):
            raise ProviderError(f"{container.container_id} boom")

    class Registry(ProviderRegistry):
        def __init__(self, **kwargs):
            super().__init__(defaults=False, **kwargs)
            for name in ("claude", "agy"):
                self.register(name, FailingProvider(name))

    class Generator:
        def __init__(self, db, **kwargs):
            self.db = db

        async def refresh_catalog(self):
            for c in await self.db.list_containers():
                await self.db.upsert_container(c.model_copy(update={"active": False}))
            for c in (
                AgentContainer(
                    container_id="architect_claude", name="Architect", role="architect",
                    provider="claude", model="claude-sonnet-4-5", active=True,
                ),
                AgentContainer(
                    container_id="repo_auditor_agy", name="Repo Agy", role="repo_auditor",
                    provider="agy", model="Gemini 4 Neon (High)", active=True,
                ),
            ):
                await self.db.upsert_container(c)
                await self.db.upsert_capability(
                    EndpointCapability(provider=c.provider, model_name=c.model or "default", healthy=True)
                )

        def start(self):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr(server, "ProviderRegistry", Registry)
    app = server.create_app(
        tmp_path / "state" / "dagger.sqlite3",
        tmp_path / "state" / "checkpoints.sqlite3",
        generator_factory=Generator,
        container_dir=tmp_path / "containers",
        project=project_dir,
    )
    with TestClient(app) as http:
        resp = http.post("/api/tasks", json={"title": "Doomed plan", "auto_start": True})
        assert resp.status_code == 201
        task_id = resp.json()["task_id"]
        for _ in range(100):
            board = http.get("/api/tasks").json()
            if board[0]["status"] in {"Review", "Failed"}:
                break
            time.sleep(0.05)
        assert board[0]["status"] == "Failed"
        logs = http.get(f"/api/tasks/{task_id}/logs").json()["logs"]
        stderr_texts = [
            entry["text"] for entry in logs
            if entry["node_id"] == "pm_plan_node" and entry["channel"] == "stderr"
        ]
        assert any(
            "All PM planner containers failed" in text
            and "architect_claude" in text
            and "repo_auditor_agy" in text
            for text in stderr_texts
        )


def test_providers_status_all_9_models_and_refresh_endpoint(tmp_path, monkeypatch):
    from dagger.providers.discovery import DiscoveryResult
    from dagger.schema import AvailableModel

    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))

    class StubGenerator:
        def __init__(self, db, **kwargs):
            self.db = db
            self.last_discovery: dict[str, DiscoveryResult] = {}
            self.refreshed_providers: list[str] = []

        async def refresh_catalog(self):
            groq_models = [
                AvailableModel(
                    provider_id="groq",
                    model_id="llama-3.3-70b-versatile",
                    display_name="llama-3.3-70b-versatile",
                    available=True,
                    is_llm=True,
                    context_window=131072,
                    discovery_source="live",
                ),
                AvailableModel(
                    provider_id="groq",
                    model_id="whisper-large-v3",
                    display_name="whisper-large-v3",
                    available=True,
                    is_llm=False,
                    discovery_source="live",
                ),
                AvailableModel(
                    provider_id="groq",
                    model_id="retired-model",
                    display_name="retired-model",
                    available=False,
                    is_llm=True,
                    discovery_source="live",
                ),
            ]
            tavily_models = [
                AvailableModel(
                    provider_id="tavily",
                    model_id="tavily-search",
                    display_name="Tavily Search",
                    available=True,
                    is_llm=False,
                    discovery_source="static",
                ),
            ]
            await self.db.replace_provider_models("groq", groq_models)
            await self.db.replace_provider_models("tavily", tavily_models)
            self.last_discovery["groq"] = DiscoveryResult(
                provider_id="groq",
                status="configured",
                detail="Groq API verified (1 LLM models discovered)",
                models=groq_models,
                discovery_status="ok",
            )
            return []

        async def refresh_provider(self, provider_id: str) -> DiscoveryResult:
            self.refreshed_providers.append(provider_id)
            models = [
                AvailableModel(
                    provider_id=provider_id,
                    model_id="qwen2.5-coder:14b",
                    display_name="qwen2.5-coder:14b",
                    available=True,
                    is_llm=True,
                    discovery_source="live",
                )
            ]
            await self.db.replace_provider_models(provider_id, models)
            res = DiscoveryResult(
                provider_id=provider_id,
                status="authenticated",
                detail="Healthy local endpoint (1 models)",
                models=models,
                discovery_status="ok",
                base_url="http://127.0.0.1:11434/v1",
            )
            self.last_discovery[provider_id] = res
            return res

        def start(self):
            pass

        async def aclose(self):
            pass

    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    app = server.create_app(
        tmp_path / "dagger.sqlite3",
        tmp_path / "checkpoints.sqlite3",
        generator_factory=StubGenerator,
        container_dir=tmp_path / "containers",
    )
    with TestClient(app) as http:
        resp = http.get("/api/providers/status")
        assert resp.status_code == 200
        providers = resp.json()["providers"]
        assert [p["provider_id"] for p in providers] == [
            "claude",
            "codex",
            "gemini",
            "copilot",
            "agy",
            "groq",
            "api",
            "tavily",
            "local",
        ]
        by_id = {p["provider_id"]: p for p in providers}

        # Verify first-class api and local metadata
        assert by_id["api"]["supports_base_url"] is True
        assert by_id["api"]["supports_api_key"] is True
        assert by_id["api"]["requires_api_key"] is True
        assert by_id["api"]["category"] == "llm"

        assert by_id["local"]["supports_base_url"] is True
        assert by_id["local"]["supports_api_key"] is False
        assert by_id["local"]["requires_api_key"] is False
        assert by_id["local"]["category"] == "local"

        assert by_id["tavily"]["category"] == "tool"

        # Available models only includes available=True entries
        assert by_id["groq"]["available_model_count"] == 2
        assert {m["model_id"] for m in by_id["groq"]["available_models"]} == {
            "llama-3.3-70b-versatile",
            "whisper-large-v3",
        }
        assert by_id["groq"]["discovery_status"] == "ok"
        assert by_id["groq"]["last_discovered_at"] is not None

        # GET /api/models query filtering
        all_models = http.get("/api/models").json()["models"]
        assert len(all_models) == 4
        groq_avail_llm = http.get(
            "/api/models",
            params={"provider_id": "groq", "available_only": True, "llm_only": True},
        ).json()["models"]
        assert [m["model_id"] for m in groq_avail_llm] == ["llama-3.3-70b-versatile"]

        llm_only = http.get("/api/models", params={"llm_only": True}).json()["models"]
        assert all(m["is_llm"] is True for m in llm_only)
        assert all(m["model_id"] != "tavily-search" for m in llm_only)

        # POST /api/providers/{id}/refresh
        bad_refresh = http.post("/api/providers/not-a-provider/refresh")
        assert bad_refresh.status_code == 400

        local_refresh = http.post("/api/providers/local/refresh")
        assert local_refresh.status_code == 200
        body = local_refresh.json()
        assert body["provider_id"] == "local"
        assert body["status"] == "authenticated"
        assert body["available_model_count"] == 1
        assert body["available_models"][0]["model_id"] == "qwen2.5-coder:14b"
        assert app.state.generator.refreshed_providers == ["local"]


def test_copilot_login_command_env_isolation_and_agy_gemini_login_flows(tmp_path, monkeypatch):
    from dagger.providers.discovery import DiscoveryResult
    from dagger.schema import AvailableModel

    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))

    class StubGenerator:
        def __init__(self, db, **kwargs):
            self.db = db
            self.last_discovery: dict[str, DiscoveryResult] = {}
            self.next_results: dict[str, DiscoveryResult] = {}
            self.refresh_calls: list[str] = []

        async def refresh_catalog(self):
            return []

        async def refresh_provider(self, provider_id: str) -> DiscoveryResult:
            self.refresh_calls.append(provider_id)
            res = self.next_results[provider_id]
            await self.db.replace_provider_models(provider_id, res.models)
            self.last_discovery[provider_id] = res
            return res

        def start(self):
            pass

        async def aclose(self):
            pass

    app = server.create_app(
        tmp_path / "dagger.sqlite3",
        tmp_path / "checkpoints.sqlite3",
        generator_factory=StubGenerator,
        container_dir=tmp_path / "containers",
    )
    with TestClient(app) as http:
        # 1. Copilot uses ["copilot", "login"] (NOT "copilot auth login") and strips metered + COPILOT_* env vars
        monkeypatch.setenv("GROQ_API_KEY", "gsk_secret")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghu_secret")
        monkeypatch.setenv("COPILOT_PROVIDER_API_KEY", "cp_secret")
        monkeypatch.setenv("COPILOT_PROVIDER_BEARER_TOKEN", "cp_bearer")
        monkeypatch.setenv("COPILOT_PROVIDER_BASE_URL", "https://override.example")

        captured_cmds: list[tuple[str, ...]] = []
        captured_envs: list[dict[str, str]] = []

        class FakeProc:
            returncode = None

            async def communicate(self):
                return (b"Open https://github.com/login/device and enter ABCD-1234\n", b"")

            def terminate(self):
                self.returncode = -15

            async def wait(self):
                return 0

        async def fake_exec(*args, **kwargs):
            captured_cmds.append(args)
            captured_envs.append(kwargs.get("env", {}))
            return FakeProc()

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd == "copilot" else None)
        monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

        resp = http.post("/api/providers/copilot/login")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "awaiting_callback"
        assert body["requires_callback"] is True
        assert body["device_code"] == "ABCD-1234"
        assert captured_cmds[0] == ("copilot", "login")
        for forbidden in (
            "GROQ_API_KEY",
            "COPILOT_GITHUB_TOKEN",
            "COPILOT_PROVIDER_API_KEY",
            "COPILOT_PROVIDER_BEARER_TOKEN",
            "COPILOT_PROVIDER_BASE_URL",
        ):
            assert forbidden not in captured_envs[0]

        # 2. agy & gemini missing binary -> HTTP 400
        assert http.post("/api/providers/agy/login").status_code == 400
        assert http.post("/api/providers/gemini/login").status_code == 400

        # 3. agy present & authenticated -> status="authenticated", requires_callback=False, available_models
        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}" if cmd in ("agy", "gemini") else None)
        agy_model = AvailableModel(
            provider_id="agy",
            model_id="gemini-4-neon-high",
            display_name="Gemini 4 Neon (High)",
            available=True,
            is_llm=True,
            discovery_source="live",
        )
        app.state.generator.next_results["agy"] = DiscoveryResult(
            provider_id="agy",
            status="authenticated",
            detail="Found at /usr/bin/agy (1 models)",
            models=[agy_model],
            discovery_status="ok",
        )
        agy_ok = http.post("/api/providers/agy/login")
        assert agy_ok.status_code == 200
        agy_body = agy_ok.json()
        assert agy_body["status"] == "authenticated"
        assert agy_body["requires_callback"] is False
        assert len(agy_body["available_models"]) == 1
        assert agy_body["provider_status"]["available_model_count"] == 1

        # 4. gemini present & unauthenticated -> status="manual_action_required", requires_callback=False
        app.state.generator.next_results["gemini"] = DiscoveryResult(
            provider_id="gemini",
            status="unauthenticated",
            detail="gemini CLI found, but no subscription OAuth/GCA/Vertex credentials configured",
            models=[
                AvailableModel(
                    provider_id="gemini",
                    model_id="gemini-2.5-pro",
                    display_name="gemini-2.5-pro",
                    available=False,
                    is_llm=True,
                    discovery_source="live",
                )
            ],
            discovery_status="ok",
        )
        gem_manual = http.post("/api/providers/gemini/login")
        assert gem_manual.status_code == 200
        gem_body = gem_manual.json()
        assert gem_body["status"] == "manual_action_required"
        assert gem_body["requires_callback"] is False
        assert "Check Login Status" in gem_body["message"]
        assert gem_body["available_models"] == []

        # 5. Empty-input /login/complete verifies live auth and discovers models even without active login session
        app.state.generator.next_results["gemini"] = DiscoveryResult(
            provider_id="gemini",
            status="authenticated",
            detail="Found at /usr/bin/gemini (1 models)",
            models=[
                AvailableModel(
                    provider_id="gemini",
                    model_id="gemini-2.5-pro",
                    display_name="gemini-2.5-pro",
                    available=True,
                    is_llm=True,
                    discovery_source="live",
                )
            ],
            discovery_status="ok",
        )
        check_resp = http.post("/api/providers/gemini/login/complete", json={"input": ""})
        assert check_resp.status_code == 200
        checked = check_resp.json()
        assert checked["provider_id"] == "gemini"
        assert checked["status"] == "authenticated"
        assert checked["available_model_count"] == 1
        assert checked["available_models"][0]["model_id"] == "gemini-2.5-pro"


def test_credentials_for_local_and_api_clearing_base_url_and_downshift_400(tmp_path, monkeypatch):
    from dagger.providers.discovery import DiscoveryResult
    from dagger.schema import AvailableModel
    from dagger.vault import get_base_url

    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))

    class StubGenerator:
        def __init__(self, db, **kwargs):
            self.db = db
            self.last_discovery: dict[str, DiscoveryResult] = {}

        async def refresh_catalog(self):
            return []

        async def refresh_provider(self, provider_id: str) -> DiscoveryResult:
            if provider_id == "local":
                url = get_base_url("local") or "http://127.0.0.1:8080/v1"
                models = [
                    AvailableModel(
                        provider_id="local",
                        model_id="llama3.1:8b",
                        display_name="llama3.1:8b",
                        available=True,
                        is_llm=True,
                        discovery_source="live",
                    )
                ]
                res = DiscoveryResult(
                    provider_id="local",
                    status="authenticated",
                    detail=f"Healthy local endpoint at {url}",
                    models=models,
                    discovery_status="ok",
                    base_url=url,
                )
            else:
                url = get_base_url("api")
                models = (
                    [
                        AvailableModel(
                            provider_id="api",
                            model_id="gpt-4.1-mini",
                            display_name="gpt-4.1-mini",
                            available=True,
                            is_llm=True,
                            discovery_source="live",
                        )
                    ]
                    if url
                    else []
                )
                res = DiscoveryResult(
                    provider_id=provider_id,
                    status="configured",
                    detail="Configured",
                    models=models,
                    discovery_status="ok" if url else "unverified",
                    base_url=url,
                )
            await self.db.replace_provider_models(provider_id, res.models)
            self.last_discovery[provider_id] = res
            return res

        async def downshift_container(self, container_id: str, target_model=None, target_container_id=None):
            if container_id == "missing_container":
                raise ValueError(f"unknown container: {container_id}")
            raise ValueError(f"Model {target_model!r} is not available for provider 'groq'")

        def start(self):
            pass

        async def aclose(self):
            pass

    app = server.create_app(
        tmp_path / "dagger.sqlite3",
        tmp_path / "checkpoints.sqlite3",
        generator_factory=StubGenerator,
        container_dir=tmp_path / "containers",
    )
    with TestClient(app) as http:
        # 1. Local provider: keyless + base_url succeeds and discovers models
        local_resp = http.post(
            "/api/providers/credentials",
            json={"provider": "local", "base_url": "http://127.0.0.1:11434/v1"},
        )
        assert local_resp.status_code == 200
        local_body = local_resp.json()
        assert local_body["status"] == "saved"
        assert local_body["provider"] == "local"
        assert local_body["available_model_count"] == 1
        assert local_body["available_models"][0]["model_id"] == "llama3.1:8b"
        assert local_body["provider_status"]["base_url"] == "http://127.0.0.1:11434/v1"
        assert get_base_url("local") == "http://127.0.0.1:11434/v1"

        # Invalid local base_url -> HTTP 400
        bad_local = http.post(
            "/api/providers/credentials",
            json={"provider": "local", "base_url": "http://user:pass@127.0.0.1:11434/v1"},
        )
        assert bad_local.status_code == 400

        # 2. Custom API: save api_key + base_url, then clear base_url with ""
        api_resp = http.post(
            "/api/providers/credentials",
            json={"provider": "api", "api_key": "sk-test-123", "base_url": "https://api.example.test/v1"},
        )
        assert api_resp.status_code == 200
        assert api_resp.json()["available_model_count"] == 1
        assert api_resp.json()["provider_status"]["base_url"] == "https://api.example.test/v1"

        clear_resp = http.post(
            "/api/providers/credentials",
            json={"provider": "api", "api_key": "sk-test-123", "base_url": ""},
        )
        assert clear_resp.status_code == 200
        assert clear_resp.json()["available_model_count"] == 0
        assert clear_resp.json()["provider_status"]["base_url"] is None
        assert get_base_url("api") is None

        # 3. Cross-provider downshift raises ValueError -> HTTP 400 with detail
        ds_bad = http.post(
            "/api/containers/groq_fast_router/downshift",
            json={"target_model": "claude-haiku-4-5"},
        )
        assert ds_bad.status_code == 400
        assert "not available for provider" in ds_bad.json()["detail"]

        # Unknown container still returns HTTP 404
        ds_missing = http.post(
            "/api/containers/missing_container/downshift",
            json={"target_model": "llama-3.1-8b-instant"},
        )
        assert ds_missing.status_code == 404
