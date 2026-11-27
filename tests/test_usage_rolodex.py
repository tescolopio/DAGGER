"""Tests for zero-token usage ledger, 0o600 credential vault isolation, Agent Rolodex, and downshift."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from dagger.agents.generator import AgentGenerator
from dagger.agents.project_manager import ProjectManager
from dagger.db import DatabaseManager
from dagger.providers import ProviderRegistry
from dagger.providers.api_runner import OpenAICompatibleApiRunner
from dagger.providers.cli_runners import ClaudeCliRunner, CodexCliRunner, CopilotCliRunner, GeminiCliRunner
from dagger.schema import AgentContainer, DefinitionOfDone, KanbanTask, TaskEnvelope, UsageEvent
from dagger.server import create_app
from dagger.vault import get_vault_path, load_vault_credentials, save_provider_credentials


@pytest.mark.asyncio
async def test_usage_events_logging_and_rolling_summary(tmp_path: Path):
    db = DatabaseManager(tmp_path / "dagger.sqlite3")
    await db.initialize()
    try:
        now = datetime.now(timezone.utc)
        await db.record_usage_event(
            UsageEvent(
                run_id="r1",
                kanban_id="k1",
                container_id="claude_architect",
                provider="claude",
                model="claude-sonnet-4-5",
                cost_tier="flat_subscription",
                estimated_cost_usd=0.0,
            )
        )
        await db.record_usage_event(
            UsageEvent(
                run_id="r2",
                kanban_id="k1",
                container_id="claude_architect",
                provider="claude",
                model="claude-sonnet-4-5",
                cost_tier="flat_subscription",
                estimated_cost_usd=0.0,
            )
        )
        await db.record_usage_event(
            UsageEvent(
                run_id="r3",
                kanban_id="k2",
                container_id="groq_fast_router",
                provider="groq",
                model="llama-3.3-70b-versatile",
                cost_tier="metered_api",
                estimated_cost_usd=0.025,
            )
        )
        events = await db.list_usage_events()
        assert len(events) == 3

        summary = await db.get_usage_summary()
        assert summary["total_events"] == 3
        claude_sub = next(s for s in summary["subscriptions"] if s["provider"] == "claude")
        assert claude_sub["calls_3h"] == 2
        assert claude_sub["calls_24h"] == 2
        assert summary["metered"]["total_calls"] == 1
        assert summary["metered"]["total_cost_usd"] == pytest.approx(0.025, abs=1e-6)
        assert summary["metered"]["cost_7d_usd"] == pytest.approx(0.025, abs=1e-6)
    finally:
        await db.close()


def test_vault_0o600_isolation_and_cli_env_stripping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault_file = tmp_path / "creds" / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))

    save_provider_credentials("groq", "gsk_test_secret_123")
    save_provider_credentials("tavily", "tvly_test_secret_456")

    assert get_vault_path() == vault_file
    mode = stat.S_IMODE(vault_file.stat().st_mode)
    assert mode == 0o600

    loaded = load_vault_credentials()
    assert loaded["GROQ_API_KEY"] == "gsk_test_secret_123"
    assert loaded["TAVILY_API_KEY"] == "tvly_test_secret_456"

    # Verify CLI runners strip GROQ_API_KEY and TAVILY_API_KEY from subprocess env
    monkeypatch.setenv("GROQ_API_KEY", "gsk_leaked")
    monkeypatch.setenv("TAVILY_API_KEY", "tvly_leaked")
    for provider_name, runner_cls in (
        ("claude", ClaudeCliRunner),
        ("codex", CodexCliRunner),
        ("gemini", GeminiCliRunner),
        ("copilot", CopilotCliRunner),
    ):
        runner = runner_cls()
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b"ok", b""))
        captured_envs: list[dict[str, str]] = []

        async def fake_exec(*args, **kwargs):
            captured_envs.append(kwargs.get("env", {}))
            return fake_proc

        container = AgentContainer(
            container_id=f"{provider_name}_test",
            name=f"{provider_name} Test",
            provider=provider_name,
            model="test-model",
            role="worker",
            cost_tier="flat_subscription",
        )
        envelope = TaskEnvelope(
            task_id="t1",
            run_id="r1",
            kanban_id="k1",
            container_id=container.container_id,
            prompt="hi",
            dod=DefinitionOfDone(acceptance_criteria=["ok"], verification_commands=["true"]),
        )
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec), patch(
            "shutil.which", return_value=f"/usr/bin/{provider_name}"
        ):
            import asyncio

            asyncio.run(runner.run(container, envelope))
        assert captured_envs
        assert "GROQ_API_KEY" not in captured_envs[0]
        assert "TAVILY_API_KEY" not in captured_envs[0]
        assert "DAGGER_API_KEY" not in captured_envs[0]


@pytest.mark.asyncio
async def test_generator_discovers_vault_containers_and_rolodex_and_downshift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))
    save_provider_credentials("groq", "gsk_test_123")
    save_provider_credentials("tavily", "tvly_test_456")

    db = DatabaseManager(tmp_path / "dagger.sqlite3")
    await db.initialize()
    try:
        gen = AgentGenerator(
            db,
            export_dir=tmp_path / "containers",
            job_store_path=tmp_path / "scheduler.sqlite3",
        )
        with patch.object(gen, "_probe_local", new=AsyncMock(return_value=[])), patch.object(
            gen, "discover_web", new=AsyncMock(return_value=[])
        ), patch.object(gen, "_command", new=AsyncMock(return_value=(False, ""))):
            containers = await gen.refresh_catalog()

        ids = {c.container_id for c in containers}
        assert "groq_fast_router" in ids
        assert "tavily_researcher" in ids

        entries = await gen.get_rolodex(active_only=True)
        assert len(entries) >= 2
        text = gen.generate_rolodex_text(entries, active_only=True)
        assert "container_id | provider:model | tier | cost_rank | skills" in text
        assert "groq_fast_router | api:llama-3.3-70b-versatile | metered_api | r2 |" in text
        # Compactness check: < 25 words per line
        for line in text.splitlines()[1:]:
            assert len(line.split()) <= 25

        updated = await gen.downshift_container("groq_fast_router")
        assert updated.model == "llama-3.1-8b-instant"
        assert updated.version >= 2
        persisted = await db.get_container("groq_fast_router")
        assert persisted is not None
        assert persisted.model == "llama-3.1-8b-instant"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_pm_decompose_goal_injects_rolodex_text(tmp_path: Path):
    pm = ProjectManager(workspace=tmp_path)
    task = KanbanTask(task_id="t1", title="Build API", description="Create endpoint")
    captured_prompts: list[str] = []

    async def fake_planner(prompt: str) -> str:
        captured_prompts.append(prompt)
        return json.dumps({
            "milestones": [
                {
                    "milestone_id": "m1",
                    "title": "Step 1",
                    "description": "Do step 1",
                    "required_skills": ["coding"],
                    "depends_on": [],
                    "dod": {
                        "acceptance_criteria": ["file exists"],
                        "expected_artifacts": [],
                        "verification_commands": ["true"],
                    },
                },
                {
                    "milestone_id": "m2",
                    "title": "Step 2",
                    "description": "Do step 2",
                    "required_skills": ["testing"],
                    "depends_on": ["m1"],
                    "dod": {
                        "acceptance_criteria": ["tests pass"],
                        "expected_artifacts": [],
                        "verification_commands": ["true"],
                    },
                },
            ]
        })

    rolodex = "container_id | provider:model | tier | cost_rank | skills\ngroq_fast_router | groq:llama-3.3-70b-versatile | metered_api | r2 | triage,routing"
    dag = await pm.decompose_goal(task, executor=fake_planner, rolodex_text=rolodex)
    assert len(dag.milestones) == 2
    assert len(captured_prompts) == 1
    assert "Available Agent Rolodex:" in captured_prompts[0]
    assert "groq_fast_router" in captured_prompts[0]


def test_server_provider_status_login_rolodex_downshift_and_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))
    app = create_app(db_path=tmp_path / "dagger.sqlite3", container_dir=tmp_path / "containers")
    with TestClient(app) as client:
        status_resp = client.get("/api/providers/status")
        assert status_resp.status_code == 200
        providers = status_resp.json()["providers"]
        provider_ids = {p["provider_id"] for p in providers}
        assert {"claude", "codex", "gemini", "copilot", "agy", "groq", "tavily", "local"}.issubset(provider_ids)

        class _FakeLoginProc:
            returncode = 0
            async def communicate(self):
                return (b"ok", b"")

        monkeypatch.setattr("shutil.which", lambda cmd: f"/bin/{cmd}" if cmd == "claude" else None)
        monkeypatch.setattr("asyncio.create_subprocess_exec", AsyncMock(return_value=_FakeLoginProc()))
        login_resp = client.post("/api/providers/claude/login")
        assert login_resp.status_code == 200
        assert login_resp.json()["provider_id"] == "claude"

        rolodex_resp = client.get("/api/rolodex")
        assert rolodex_resp.status_code == 200
        data = rolodex_resp.json()
        assert "entries" in data and "text" in data

        usage_resp = client.get("/api/usage/summary")
        assert usage_resp.status_code == 200
        assert "subscriptions" in usage_resp.json()

        target_entry = data["entries"][0]
        target_id = target_entry["container_id"]
        ds_resp = client.post(f"/api/containers/{target_id}/downshift", json={})
        assert ds_resp.status_code == 200
        assert ds_resp.json()["container"]["container_id"] == target_id
        assert ds_resp.json()["container"]["model"]
        # Cross-provider model must be rejected when target provider is not claude.
        if target_entry.get("service_provider", target_entry.get("provider")) != "claude":
            bad_ds = client.post(
                f"/api/containers/{target_id}/downshift",
                json={"target_model": "claude-haiku-4-5"},
            )
            assert bad_ds.status_code in (400, 404)

        cred_resp = client.post(
            "/api/providers/credentials",
            json={"provider": "groq", "api_key": "gsk_server_saved_123"},
        )
        assert cred_resp.status_code == 200
        assert cred_resp.json()["status"] == "saved"
        assert cred_resp.json()["provider"] == "groq"

        # Verify POST /api/providers/claude/login strips metered/token env vars
        monkeypatch.setenv("GROQ_API_KEY", "gsk_leaked")
        monkeypatch.setenv("DAGGER_API_KEY", "dag_leaked")
        captured_login_envs: list[dict[str, str]] = []
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b"ok", b""))

        async def fake_login_exec(*args, **kwargs):
            captured_login_envs.append(kwargs.get("env", {}))
            return fake_proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_login_exec), patch(
            "shutil.which", return_value="/usr/bin/claude"
        ):
            login_env_resp = client.post("/api/providers/claude/login")
            assert login_env_resp.status_code == 200
        if captured_login_envs:
            assert "GROQ_API_KEY" not in captured_login_envs[0]
            assert "DAGGER_API_KEY" not in captured_login_envs[0]


def test_server_interactive_oauth_login_and_complete(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import asyncio

    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))
    app = create_app(db_path=tmp_path / "dagger.sqlite3", container_dir=tmp_path / "containers")

    class _FakeStream:
        def __init__(self, chunks: list[bytes]):
            self._chunks = list(chunks)

        async def read(self, _n: int = 4096) -> bytes:
            if self._chunks:
                return self._chunks.pop(0)
            await asyncio.sleep(0.05)
            return b""

    class _FakeStdin:
        def __init__(self):
            self.written: list[bytes] = []

        def write(self, data: bytes) -> None:
            self.written.append(data)

        async def drain(self) -> None:
            return None

    class _FakeInteractiveProc:
        def __init__(self, stdout_chunks: list[bytes], stderr_chunks: list[bytes] | None = None):
            self.returncode = None
            self.stdin = _FakeStdin()
            self.stdout = _FakeStream(stdout_chunks)
            self.stderr = _FakeStream(stderr_chunks or [])
            self.terminated = False

        async def wait(self) -> int:
            self.returncode = 0
            return 0

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def kill(self) -> None:
            self.returncode = -9

    with TestClient(app) as client:
        captured_envs: list[dict[str, str]] = []
        proc_callback = _FakeInteractiveProc(
            [b"Open https://auth.example.test/oauth?client=123 and enter code ABCD-1234.\n"]
        )

        async def fake_exec_callback(*args, **kwargs):
            captured_envs.append(kwargs.get("env", {}))
            return proc_callback

        monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec_callback):
            resp = client.post("/api/providers/claude/login")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "awaiting_callback"
            assert body["auth_url"] == "https://auth.example.test/oauth?client=123"
            assert body["device_url"] == "https://auth.example.test/oauth?client=123"
            assert body["device_code"] == "ABCD-1234"
            assert captured_envs[0].get("BROWSER") == "echo"

        # 1. Complete via localhost callback URL -> rewritten to 127.0.0.1 and relayed via httpx
        relayed_urls: list[str] = []

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def get(self, url: str):
                relayed_urls.append(url)
                return MagicMock(status_code=200)

        with patch("httpx.AsyncClient", _FakeAsyncClient), patch.object(
            app.state.generator, "refresh_catalog", new=AsyncMock()
        ) as mock_refresh:
            complete_resp = client.post(
                "/api/providers/claude/login/complete",
                json={"input": "http://localhost:54321/callback?code=xyz&state=abc"},
            )
            assert complete_resp.status_code == 200
            assert relayed_urls == ["http://127.0.0.1:54321/callback?code=xyz&state=abc"]
            mock_refresh.assert_awaited_once()
            status_body = complete_resp.json()
            assert status_body["provider_id"] == "claude"
            assert "status" in status_body

        # 2. Complete via plain authorization/device code -> written to subprocess stdin
        proc_code = _FakeInteractiveProc([b"Visit https://github.com/login/device code WXYZ-5678\n"])

        async def fake_exec_code(*args, **kwargs):
            return proc_code

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec_code):
            resp2 = client.post("/api/providers/copilot/login")
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "awaiting_callback"

        with patch.object(app.state.generator, "refresh_catalog", new=AsyncMock()) as mock_refresh2:
            complete_code_resp = client.post(
                "/api/providers/copilot/login/complete",
                json={"input": "WXYZ-5678"},
            )
            assert complete_code_resp.status_code == 200
            assert proc_code.stdin.written == [b"WXYZ-5678\n"]
            mock_refresh2.assert_awaited_once()
            assert complete_code_resp.json()["provider_id"] == "copilot"

        # 3. SSRF rejection: userinfo (@evil.com) and non-localhost hostnames must return 400 and never call httpx
        class _SlowStream:
            def __init__(self, delay: float, data: bytes):
                self._delay = delay
                self._data = data
                self._sent = False

            async def read(self, _n: int = 4096) -> bytes:
                if not self._sent:
                    await asyncio.sleep(self._delay)
                    self._sent = True
                    return self._data
                await asyncio.sleep(0.05)
                return b""

        proc_slow = _FakeInteractiveProc([])
        proc_slow.stdout = _SlowStream(0.35, b"Visit https://auth.example.test/slow?x=1\n")
        captured_kwargs: list[dict] = []

        async def fake_exec_slow(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return proc_slow

        with patch("asyncio.create_subprocess_exec", side_effect=fake_exec_slow):
            slow_resp = client.post("/api/providers/codex/login")
            assert slow_resp.status_code == 200
            assert slow_resp.json()["auth_url"] == "https://auth.example.test/slow?x=1"
            assert captured_kwargs[0].get("stdin") == asyncio.subprocess.PIPE

        # Re-login terminates stale prior session
        proc_stale_replacement = _FakeInteractiveProc([b"https://auth.example.test/new\n"])
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=proc_stale_replacement)):
            relogin_resp = client.post("/api/providers/codex/login")
            assert relogin_resp.status_code == 200
            assert proc_slow.terminated is True

        with patch("httpx.AsyncClient") as mock_http_cls:
            for bad_url in (
                "http://localhost:80@evil.com/cb",
                "http://127.0.0.1:80@evil.com/cb",
                "http://localhost.evil.com:8080/cb",
                "http://localhost:99999/cb",
            ):
                bad_resp = client.post(
                    "/api/providers/codex/login/complete",
                    json={"input": bad_url},
                )
                assert bad_resp.status_code == 400
            mock_http_cls.assert_not_called()


@pytest.mark.asyncio
async def test_registry_automatic_usage_logging_and_local_free_isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault_file = tmp_path / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))
    save_provider_credentials("api", "dag_secret_999", base_url="https://api.example.test/v1")

    db = DatabaseManager(tmp_path / "dagger.sqlite3")
    await db.initialize()
    try:
        registry = ProviderRegistry(db=db)
        cli_container = AgentContainer(
            container_id="claude_test",
            name="Claude Test",
            provider="claude",
            model="claude-sonnet-4-5",
            role="worker",
            cost_tier="flat_subscription",
        )
        envelope = TaskEnvelope(
            task_id="t1",
            run_id="r_cli",
            kanban_id="k_cli",
            container_id="claude_test",
            prompt="hello",
            dod=DefinitionOfDone(acceptance_criteria=["ok"], verification_commands=["true"]),
        )
        fake_proc = MagicMock()
        fake_proc.returncode = 0
        fake_proc.communicate = AsyncMock(return_value=(b'{"result":"cli ok"}', b""))
        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=fake_proc)), patch(
            "shutil.which", return_value="/usr/bin/claude"
        ):
            out = await registry.get("claude").run(
                cli_container,
                envelope,
                usage_context={
                    "run_id": "r_cli",
                    "kanban_id": "k_cli",
                    "container_id": "claude_test",
                    "cost_tier": "flat_subscription",
                },
            )
        assert out == "cli ok"

        # HTTP local_free container must NOT attach vault DAGGER_API_KEY or override base_url
        local_container = AgentContainer(
            container_id="local_triage",
            name="Local Triage",
            provider="api",
            base_url="http://127.0.0.1:8080/v1",
            model="local-model",
            role="triage",
            cost_tier="local_free",
        )
        captured_headers: list[dict[str, str]] = []
        captured_urls: list[str] = []

        class FakeResponse:
            status_code = 200
            is_error = False

            def raise_for_status(self):
                return None

            def json(self):
                return {"choices": [{"message": {"content": "local ok"}}]}

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json=None, headers=None):
                captured_urls.append(url)
                captured_headers.append(headers or {})
                return FakeResponse()

        with patch("httpx.AsyncClient", FakeAsyncClient):
            http_out = await registry.get("api").run(
                local_container,
                envelope.model_copy(update={"container_id": "local_triage", "run_id": "r_http"}),
                usage_context={
                    "run_id": "r_http",
                    "kanban_id": "k_cli",
                    "container_id": "local_triage",
                    "cost_tier": "local_free",
                },
            )
        assert http_out == "local ok"
        assert captured_urls[0].startswith("http://127.0.0.1:8080/v1")
        assert "Authorization" not in captured_headers[0]

        events = await db.list_usage_events()
        assert len(events) == 2
        assert {e.container_id for e in events} == {"claude_test", "local_triage"}
    finally:
        await db.close()


def test_vault_default_path_and_dotenv_auto_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DAGGER_CREDENTIALS_FILE", raising=False)
    monkeypatch.delenv("DAGGER_DOTENV_FILE", raising=False)
    default_path = get_vault_path()
    assert default_path.parts[-2:] == (".dagger", "credentials.env")

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    dotenv_file = repo_dir / ".env"
    dotenv_file.write_text(
        "GROQ_API_KEY=gsk_from_dotenv\nTAVILY_API_KEY=tvly_from_dotenv\nUNRELATED_KEY=ignored\n",
        encoding="utf-8",
    )
    vault_file = repo_dir / ".dagger" / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))

    loaded = load_vault_credentials()
    assert loaded["GROQ_API_KEY"] == "gsk_from_dotenv"
    assert loaded["TAVILY_API_KEY"] == "tvly_from_dotenv"
    assert "UNRELATED_KEY" not in loaded
    assert vault_file.is_file()
    assert stat.S_IMODE(vault_file.stat().st_mode) == 0o600


async def test_copilot_gh_auth_fallback_and_gemini_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async with DatabaseManager(tmp_path / "db.sqlite", seed_path=False) as db:
        from dagger.agents.generator import AgentGenerator

        seed = tmp_path / "seed.yaml"
        seed.write_text("containers: []\n", encoding="utf-8")
        gen = AgentGenerator(db=db, seed_path=seed, export_dir=tmp_path / "containers")

        # Copilot: copilot auth fails, gh auth status succeeds
        copilot_c = AgentContainer(
            container_id="copilot_cli",
            name="Copilot",
            provider="copilot",
            model="gpt-4.1",
            role="worker",
            cost_tier="flat_subscription",
        )

        async def fake_cmd_copilot(exe: str, *args: str):
            if exe.endswith("copilot") and args == ("--version",):
                return True, "copilot 1.0.0"
            if exe.endswith("copilot"):
                return False, "not logged in"
            if exe.endswith("gh") and args == ("auth", "status"):
                return True, "Logged in to github.com"
            return False, ""

        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name in ("copilot", "gh") else None)
        monkeypatch.setattr(gen, "_command", fake_cmd_copilot)
        cap_copilot = await gen._probe_cli(copilot_c)
        assert cap_copilot.healthy is True

        # Gemini: gemini --version succeeds + ~/.gemini/settings.json + oauth_creds.json exist
        fake_home = tmp_path / "home"
        (fake_home / ".gemini").mkdir(parents=True)
        (fake_home / ".gemini" / "settings.json").write_text('{"selectedType": "oauth-personal"}', encoding="utf-8")
        (fake_home / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(Path, "home", lambda: fake_home)

        gemini_c = AgentContainer(
            container_id="gemini_cli",
            name="Gemini",
            provider="gemini",
            model="gemini-2.5-pro",
            role="worker",
            cost_tier="flat_subscription",
        )

        async def fake_cmd_gemini(exe: str, *args: str):
            if args == ("--version",):
                return True, "gemini 1.0.0"
            return False, "auth status unsupported"

        monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}" if name == "gemini" else None)
        monkeypatch.setattr(gen, "_command", fake_cmd_gemini)
        cap_gemini = await gen._probe_cli(gemini_c)
        assert cap_gemini.healthy is True


async def test_strict_readiness_gating_and_runtime_failover(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from dagger.agents.orchestrator import Orchestrator
    from dagger.schema import DefinitionOfDone, MilestoneTask

    vault_file = tmp_path / ".dagger" / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))

    async with DatabaseManager(tmp_path / "db.sqlite", seed_path=False) as db:
        seed = tmp_path / "seed.yaml"
        seed.write_text(
            """containers:
  - container_id: local_triage
    name: Local Triage
    provider: api
    model: local-model
    role: triage
    cost_tier: local_free
    allowed_tools: [read, search]
    strengths: [triage]
    active: true
  - container_id: api_primary
    name: API Primary
    provider: api
    model: gpt-4o-mini
    role: worker
    cost_tier: metered_api
    allowed_tools: [read, write]
    strengths: [coding]
    active: true
""",
            encoding="utf-8",
        )
        from dagger.agents.generator import AgentGenerator

        gen = AgentGenerator(db=db, seed_path=seed, export_dir=tmp_path / "containers")
        monkeypatch.setattr("shutil.which", lambda _name: None)
        monkeypatch.setattr(gen, "_probe_local", AsyncMock(return_value=[]))
        monkeypatch.setattr(gen, "discover_web", AsyncMock(return_value=[]))

        containers = await gen.refresh_catalog()
        by_id = {c.container_id: c for c in containers}
        assert by_id["local_triage"].active is False
        assert by_id["api_primary"].active is False
        assert await gen.get_rolodex() == []

        # Now test runtime failover in Orchestrator._work()
        primary = AgentContainer(
            container_id="primary_worker",
            name="Primary",
            provider="api",
            model="gpt-4o-mini",
            role="worker",
            cost_tier="metered_api",
            allowed_tools=["read", "write"],
            strengths=["coding"],
            fallback_providers=["claude"],
            active=True,
        )
        fallback = AgentContainer(
            container_id="claude_fallback",
            name="Claude Fallback",
            provider="claude",
            model="claude-sonnet-4-5",
            role="worker",
            cost_tier="flat_subscription",
            allowed_tools=["read", "write"],
            strengths=["coding"],
            active=True,
        )
        await db.upsert_container(primary)
        await db.upsert_container(fallback)
        from dagger.schema import AvailableModel

        await db.replace_provider_models(
            "claude",
            [
                AvailableModel(
                    provider_id="claude",
                    model_id="claude-sonnet-4-5",
                    available=True,
                    is_llm=True,
                    discovery_source="curated",
                )
            ],
        )

        class FailingRunner:
            async def run(self, container, envelope, usage_context=None):
                raise RuntimeError("missing credential")

        class SucceedingRunner:
            async def run(self, container, envelope, usage_context=None):
                return "fallback succeeded"

        class FakeRegistry:
            def get(self, provider: str):
                return FailingRunner() if provider == "api" else SucceedingRunner()

        events: list[tuple[str, dict]] = []

        async def emit(run_id, kanban_id, event_type, payload):
            events.append((event_type, payload))

        orch = Orchestrator(
            db,
            FakeRegistry(),
            tmp_path,
            bus_path=tmp_path / "bus",
        )
        orch._emit = emit
        milestone = MilestoneTask(
            milestone_id="m1",
            title="Test",
            description="Test failover",
            required_skills=["coding"],
            assigned_container_id="primary_worker",
            dod=DefinitionOfDone(acceptance_criteria=[], expected_artifacts=[], verification_commands=[]),
        )
        # Ensure select() excludes inactive containers
        selected = await orch.select(milestone)
        assert selected.container_id == "primary_worker"

        state = {
            "run_id": "run_1",
            "kanban_id": "kb_1",
            "task": {**milestone.model_dump(mode="json"), "_hop_count": 0},
        }
        result = await orch._work(state)
        assert result == "fallback succeeded"
        assert any(
            etype == "EDGE_TRAVERSED" and payload.get("fallback") is True and payload.get("container_id") == "claude_fallback"
            for etype, payload in events
        )


async def test_dagger_vault_blocked_from_artifact_materialization(tmp_path: Path):
    from dagger.agents.orchestrator import Orchestrator

    async with DatabaseManager(tmp_path / "db.sqlite", seed_path=False) as db:
        orch = Orchestrator(
            db,
            MagicMock(),
            tmp_path,
            bus_path=tmp_path / "bus",
        )
        with pytest.raises(ValueError, match="\\.dagger"):
            orch._resolve_worktree_target(".dagger/credentials.env")



@pytest.mark.asyncio
async def test_agy_swarm_squad_streaming_usage_oauth_and_boundary_failover(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from dagger.agents.generator import AgentGenerator
    from dagger.agents.orchestrator import Orchestrator
    from dagger.providers.cli_runners import AgyCliRunner
    from dagger.schema import AgentContainer, DefinitionOfDone, TaskEnvelope

    async with DatabaseManager(tmp_path / "db.sqlite", seed_path=False) as db:
        gen = AgentGenerator(db=db, export_dir=tmp_path / "exports", job_store_path=tmp_path / "jobs.sqlite")
        monkeypatch.setattr("dagger.agents.generator.shutil.which", lambda _cmd: None)
        monkeypatch.setattr(gen, "_probe_local", AsyncMock(return_value=[]))
        monkeypatch.setattr(gen, "discover_web", AsyncMock(return_value=[]))
        containers = await gen.refresh_catalog()
        by_id = {c.container_id: c for c in containers}
        assert by_id["agy_swarm_squad"].active is False

        monkeypatch.setattr("dagger.agents.generator.shutil.which", lambda cmd: f"/bin/{cmd}" if cmd == "agy" else None)
        monkeypatch.setattr(gen, "_command", AsyncMock(return_value=(True, "1.5.14")))
        containers = await gen.refresh_catalog()
        by_id = {c.container_id: c for c in containers}
        assert by_id["agy_swarm_squad"].active is True

    usage_events = []
    runner = AgyCliRunner(usage_callback=usage_events.append)
    container = AgentContainer(
        container_id="agy_swarm_squad", name="Agy Swarm Squad", role="swarm_squad",
        provider="agy", model="gemini-4-neon",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-secret")

    class FakeStdout:
        def __init__(self, lines):
            self._lines = [l.encode("utf-8") for l in lines] + [b""]
        async def readline(self):
            return self._lines.pop(0)

    class FakeStderr:
        async def read(self):
            return b""

    class FakeProc:
        def __init__(self, lines):
            self.stdout = FakeStdout(lines)
            self.stderr = FakeStderr()
        async def wait(self):
            return 0

    seen_env = {}
    seen_args = []

    async def fake_exec(*args, **kwargs):
        seen_args.extend(args)
        seen_env.update(kwargs.get("env") or {})
        return FakeProc(["#1 backend: [done] finished\n"])

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)
    emitted = []
    task = TaskEnvelope(
        task_id="t1", run_id="r1", kanban_id="k1", container_id="agy_swarm_squad",
        prompt="build", allowed_write_paths=["src/allowed.py"],
        dod=DefinitionOfDone(acceptance_criteria=[]),
    )
    out = await runner.run(container, task, usage_context={"on_swarm_post": emitted.append})
    assert "#1 backend: [done] finished" in out
    assert "OPENAI_API_KEY" not in seen_env and "GEMINI_API_KEY" not in seen_env
    assert any("src/allowed.py" in str(a) for a in seen_args)
    assert emitted == [{"seq": 1, "author": "backend", "tag": "[done]", "message": "#1 backend: [done] finished"}]
    assert len(usage_events) == 1 and usage_events[0].success is True

    worktree = tmp_path / "wt"
    worktree.mkdir()
    app = Orchestrator.__new__(Orchestrator)
    app.worktree = worktree
    app._container_limits = {}
    app._last_executed_containers = {}
    app._emit = AsyncMock()
    app._update_milestone = AsyncMock()

    c_agy = AgentContainer(
        container_id="agy_swarm_squad", name="Agy", role="swarm_squad",
        provider="agy", model="gemini-4-neon", active=True, fallback_providers=["architect_claude"],
    )
    c_claude = AgentContainer(
        container_id="architect_claude", name="Claude", role="worker",
        provider="claude", model="claude-sonnet-4-5", active=True,
    )

    class DB:
        async def list_containers(self):
            return [c_agy, c_claude]
        async def list_capabilities(self):
            return []

    class ViolatingAgy:
        async def run(self, cand, env, usage_context=None):
            (worktree / "forbidden.py").write_text("bad", encoding="utf-8")
            return "violated"

    class GoodClaude:
        async def run(self, cand, env, usage_context=None):
            return "claude-ok"

    class Reg:
        def get(self, name):
            return ViolatingAgy() if name == "agy" else GoodClaude()

    app.db = DB()
    app.registry = Reg()
    assert await app._run_provider(c_agy, task) == "claude-ok"

    class StallingAgy:
        async def run(self, cand, env, usage_context=None):
            from dagger.providers.base import ProviderError
            raise ProviderError("agy inner swarm stalled")

    app.registry = type("Reg2", (), {"get": lambda _s, name: StallingAgy() if name == "agy" else GoodClaude()})()
    assert await app._run_provider(c_agy, task) == "claude-ok"


def test_agy_cli_runner_model_and_effort_normalization():
    from dagger.providers.cli_runners import AgyCliRunner
    from dagger.schema import AgentContainer

    runner = AgyCliRunner()

    cases = [
        ("Gemini 4 Neon (High)", "gemini-4-neon-high"),
        ("Gemini 4 Neon (XHigh)", "gemini-4-neon-xhigh"),
        ("Gemini 4 Hydrogen (Low)", "gemini-4-hydrogen-low"),
        ("Claude Sonnet 5.5 (Medium)", "claude-sonnet-5-5-medium"),
        ("Orchestrator (Neon)", "gemini-pro-orchestrator-neon"),
        ("Orchestrator (Carbon)", "gemini-pro-orchestrator"),
        ("Orchestrator (Argon)", "gemini-pro-orchestrator-argon"),
        ("gemini-pro-orchestrator", "gemini-pro-orchestrator"),
        ("gemini-pro-orchestrator-neon", "gemini-pro-orchestrator-neon"),
        ("gemini-pro-orchestrator-argon", "gemini-pro-orchestrator-argon"),
        ("Gemini 4 Neon", "gemini-4-neon-high"),
        ("gemini-4-neon", "gemini-4-neon-high"),
        ("gemini-4-hydrogen", "gemini-4-hydrogen-low"),
        ("gemini-4-neon-high", "gemini-4-neon-high"),
        ("gemini-3.8-flash-low", "gemini-3.8-flash-low"),
        ("gemini-3-8-flash-high", "gemini-3.8-flash-high"),
        ("claude-opus-5-5-high", "claude-opus-5-5-high"),
        ("claude-sonnet-5.5-medium", "claude-sonnet-5-5-medium"),
        ("gpt-oss-120b-medium", "gpt-oss-120b-medium"),
        ("claude-haiku-4-5", "gemini-4-hydrogen-low"),
        ("gpt-4o-mini", "gemini-4-hydrogen-low"),
        ("fast", "gemini-4-hydrogen-low"),
        ("flash", "gemini-4-hydrogen-low"),
        ("unknown-heavy-model", "gemini-4-neon-high"),
        (None, "gemini-4-neon-high"),
    ]
    for raw_model, expected_model in cases:
        assert expected_model in AgyCliRunner.SUPPORTED_MODELS
        assert runner.normalize_model_and_effort(raw_model) == (expected_model, None)
        container = AgentContainer(
            name="agy_test", role="fast_auditor", provider="agy", model=raw_model
        )
        args = runner.arguments(container, "check", None)
        assert "--effort" not in args
        assert args == [
            "agy", "-p", "check", "--model", expected_model, "--disable-slash-commands"
        ], f"Failed for {raw_model}: {args}"

    # Read-only container enforces --mode plan and never passes --effort.
    ro_container = AgentContainer(
        name="agy_ro",
        role="repo_auditor",
        provider="agy",
        model="Gemini 4 Neon (High)",
        allowed_tools=["read", "search"],
    )
    ro_args = runner.arguments(ro_container, "audit", None)
    assert ro_args == [
        "agy", "-p", "audit", "--model", "gemini-4-neon-high", "--disable-slash-commands", "--mode", "plan"
    ]

    # Swarm container keeps slash commands enabled for `/swarm` and omits --effort.
    swarm_container = AgentContainer(
        container_id="agy_swarm_squad",
        name="Agy Swarm Squad",
        role="swarm_squad",
        provider="agy",
        model="Gemini 4 Neon (High)",
        allowed_tools=["read", "search", "write", "edit"],
    )
    swarm_args = runner.arguments(swarm_container, "build", None)
    assert "--effort" not in swarm_args
    assert "--disable-slash-commands" not in swarm_args
    assert swarm_args[0:2] == ["agy", "-p"]
    assert swarm_args[2].startswith("/swarm ")
    assert swarm_args[3:] == ["--model", "gemini-4-neon-high"]


async def test_generator_seeds_valid_agy_model_effort_strings(tmp_path):
    from dagger.schema import EndpointCapability

    async def fake_probe_cli(container: AgentContainer) -> EndpointCapability:
        # Mirror the real _probe_cli contract: always return a capability, never None.
        return EndpointCapability(
            provider=container.provider, model_name=container.model or "default", healthy=False
        )

    async def no_local_models() -> list[str]:
        return []

    async def no_web_findings() -> list[dict[str, str]]:
        return []

    # Close the DB on exit: a leaked aiosqlite connection thread keeps the interpreter alive.
    async with DatabaseManager(tmp_path / "test.db") as db:
        gen = AgentGenerator(db, export_dir=tmp_path / "containers")
        gen._probe_cli = fake_probe_cli  # type: ignore[method-assign]
        gen._probe_local = no_local_models  # type: ignore[method-assign]
        gen.discover_web = no_web_findings  # type: ignore[method-assign]
        seeds = {c.container_id: c for c in await gen.refresh_catalog()}
    assert seeds["fast_auditor_agy"].model == "Gemini 4 Hydrogen (Low)"
    assert seeds["repo_auditor_agy"].model == "Gemini 4 Neon (High)"
    assert seeds["agy_swarm_squad"].model == "Gemini 4 Neon (High)"


@pytest.mark.asyncio
async def test_orchestrator_excludes_tavily_and_unavailable_discovered_models(tmp_path: Path):
    from dagger.agents.orchestrator import Orchestrator
    from dagger.schema import AvailableModel, DefinitionOfDone, MilestoneTask

    async with DatabaseManager(tmp_path / "db.sqlite", seed_path=False) as db:
        tavily_c = AgentContainer(
            container_id="tavily_researcher",
            name="Tavily Researcher",
            provider="api",
            model="tavily-search",
            role="researcher",
            cost_tier="metered_api",
            allowed_tools=["search"],
            strengths=["web_search", "research", "review"],
            active=True,
        )
        stale_gemini = AgentContainer(
            container_id="fast_auditor_gemini",
            name="Gemini Fast Auditor",
            provider="gemini",
            model="gemini-3.8-flash",
            role="fast_auditor",
            cost_tier="flat_subscription",
            allowed_tools=["read", "search"],
            strengths=["review", "analysis"],
            active=True,
        )
        valid_gemini = AgentContainer(
            container_id="repo_auditor_gemini",
            name="Gemini Repo Auditor",
            provider="gemini",
            model="gemini-2.5-pro",
            role="repo_auditor",
            cost_tier="flat_subscription",
            allowed_tools=["read", "search"],
            strengths=["review", "analysis"],
            active=True,
        )
        for c in (tavily_c, stale_gemini, valid_gemini):
            await db.upsert_container(c)
        await db.replace_provider_models(
            "gemini",
            [
                AvailableModel(
                    provider_id="gemini",
                    model_id="gemini-2.5-pro",
                    available=True,
                    is_llm=True,
                    discovery_source="live",
                ),
                AvailableModel(
                    provider_id="gemini",
                    model_id="gemini-3.5-flash",
                    available=True,
                    is_llm=True,
                    discovery_source="live",
                ),
            ],
        )
        await db.replace_provider_models(
            "tavily",
            [
                AvailableModel(
                    provider_id="tavily",
                    model_id="tavily-search",
                    available=True,
                    is_llm=False,
                    discovery_source="static",
                ),
            ],
        )

        orch = Orchestrator(db, MagicMock(), tmp_path, bus_path=tmp_path / "bus")

        # Lightweight review milestone must skip BOTH stale gemini-3.8-flash AND tavily_researcher!
        review_ms = MilestoneTask(
            milestone_id="m_review",
            title="Review",
            description="Review changes",
            required_skills=["review"],
            dod=DefinitionOfDone(acceptance_criteria=[], expected_artifacts=[], verification_commands=[]),
        )
        selected = await orch.select(review_ms)
        assert selected.container_id == "repo_auditor_gemini"

        # Explicitly requesting tavily_researcher as an LLM worker must fail closed.
        tavily_ms = review_ms.model_copy(update={"assigned_container_id": "tavily_researcher"})
        with pytest.raises(RuntimeError, match="no active matching agent container"):
            await orch.select(tavily_ms)

        # Failover must NEVER route to tavily_researcher or stale unavailable models.
        called: list[str] = []

        class FailingRunner:
            async def run(self, container, envelope, usage_context=None):
                called.append(container.container_id)
                raise RuntimeError("primary failed")

        orch.registry = type("Reg", (), {"get": lambda _s, _p: FailingRunner()})()
        orch._emit = AsyncMock()
        orch._update_milestone = AsyncMock()
        env = TaskEnvelope(
            task_id="t_fail",
            run_id="r_fail",
            kanban_id="k_fail",
            container_id="repo_auditor_gemini",
            prompt="audit",
            dod=DefinitionOfDone(acceptance_criteria=[], expected_artifacts=[], verification_commands=[]),
        )
        with pytest.raises(RuntimeError, match="primary failed"):
            await orch._run_provider(valid_gemini, env)
        assert called == ["repo_auditor_gemini"]

