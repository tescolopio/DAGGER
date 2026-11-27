"""Unit tests for non-destructive provider model discovery, OAuth isolation, and vault URL handling."""

from __future__ import annotations

import asyncio
import json
import stat
from pathlib import Path

import httpx
import pytest
import respx

from dagger.providers.cli_runners import AgyCliRunner, CopilotCliRunner
from dagger.providers.discovery import (
    CLI_PROBE_STRIPPED_ENV,
    SERVICE_PROVIDER_METADATA,
    _run_cli_probe,
    discover_provider_models,
    has_gemini_subscription_auth,
    parse_agy_models_output,
    parse_copilot_help_config_models,
    parse_gemini_models_json,
    parse_gemini_models_text,
)
from dagger.schema import AgentContainer, DefinitionOfDone, TaskEnvelope
from dagger.vault import (
    get_base_url,
    get_vault_credential,
    load_vault_credentials,
    save_provider_credentials,
    validate_base_url,
)


def test_service_provider_metadata_lists_all_nine_providers():
    ids = [item["provider_id"] for item in SERVICE_PROVIDER_METADATA]
    assert ids == ["claude", "codex", "gemini", "copilot", "agy", "groq", "api", "tavily", "local"]


def test_vault_local_keyless_url_validation_and_clearing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    vault_file = tmp_path / ".dagger" / "credentials.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))
    monkeypatch.delenv("DAGGER_LOCAL_BASE_URL", raising=False)
    monkeypatch.delenv("DAGGER_API_BASE_URL", raising=False)

    # Keyless local endpoint registration works and enforces 0o600 permissions.
    save_provider_credentials("local", "", base_url="http://127.0.0.1:11434/v1")
    assert stat.S_IMODE(vault_file.stat().st_mode) == 0o600
    assert get_base_url("local") == "http://127.0.0.1:11434/v1"
    assert get_vault_credential("DAGGER_LOCAL_API_KEY") is None

    # Clearing base_url with "" removes it from vault for both local and api.
    save_provider_credentials("local", "", base_url="")
    assert get_base_url("local") is None

    save_provider_credentials("api", "sk-test-123", base_url="https://api.example.test/v1")
    assert get_base_url("api") == "https://api.example.test/v1"
    save_provider_credentials("api", "sk-test-123", base_url="")
    assert get_base_url("api") is None
    assert load_vault_credentials()["DAGGER_API_KEY"] == "sk-test-123"

    # Reject invalid / credential-bearing URLs.
    for bad_url in (
        "http://user:pass@127.0.0.1:8080/v1",
        "https://user@example.test/v1",
        "file:///etc/passwd",
        "http://127.0.0.1:99999/v1",
        "https://example.test/v1?token=secret",
        "http://127.0.0.1:8080\nINJECTED=1",
    ):
        with pytest.raises(ValueError):
            validate_base_url(bad_url)
        with pytest.raises(ValueError):
            save_provider_credentials("local", "", base_url=bad_url)

    # Non-local API providers still require non-empty api_key.
    with pytest.raises(ValueError, match="api_key"):
        save_provider_credentials("groq", "")
    with pytest.raises(ValueError, match="base_url is required"):
        save_provider_credentials("local", "", base_url=None)


@pytest.mark.asyncio
async def test_cli_probe_and_runners_strip_metered_and_copilot_env(monkeypatch: pytest.MonkeyPatch):
    captured_envs: list[dict[str, str]] = []

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"1.0.0", b""

    async def fake_exec(*args, **kwargs):
        captured_envs.append(dict(kwargs.get("env", {})))
        return FakeProc()

    for key in CLI_PROBE_STRIPPED_ENV:
        monkeypatch.setenv(key, f"secret-{key}")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ok, rc, stdout, _ = await _run_cli_probe("agy", "--version")
    assert ok is True and rc == 0 and stdout == "1.0.0"
    for key in CLI_PROBE_STRIPPED_ENV:
        assert key not in captured_envs[-1]

    # Also verify CopilotCliRunner strips COPILOT_* BYOK/token vars via _METERED_ENV_KEYS.
    container = AgentContainer(
        name="copilot", role="patcher", provider="copilot", model="gpt-4.1"
    )
    task = TaskEnvelope(
        task_id="t1",
        run_id="r1",
        kanban_id="k1",
        container_id="c1",
        prompt="hi",
        dod=DefinitionOfDone(acceptance_criteria=[]),
    )
    await CopilotCliRunner().run(container, task)
    for key in (
        "COPILOT_GITHUB_TOKEN",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_BEARER_TOKEN",
        "COPILOT_PROVIDER_BASE_URL",
    ):
        assert key not in captured_envs[-1]


@pytest.mark.asyncio
async def test_discover_agy_live_fallback_auth_error_and_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("dagger.providers.discovery.shutil.which", lambda _: None)
    missing = await discover_provider_models("agy")
    assert missing.status == "missing_binary"
    assert missing.models == []

    monkeypatch.setattr("dagger.providers.discovery.shutil.which", lambda cmd: f"/bin/{cmd}")

    # Live discovery
    async def probe_live(*args, timeout=5.0):
        if args[1] == "--version":
            return True, 0, "1.5.18", ""
        return (
            True,
            0,
            "gemini-4-neon-high    Gemini 4 Neon (High)\ngemini-4-hydrogen-low    Gemini 4 Hydrogen (Low)\n",
            "",
        )

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_live)
    live = await discover_provider_models("agy")
    assert live.status == "authenticated"
    assert live.discovery_status == "ok"
    assert live.version == "1.5.18"
    assert [(m.model_id, m.display_name, m.available, m.discovery_source) for m in live.models] == [
        ("gemini-4-neon-high", "Gemini 4 Neon (High)", True, "live"),
        ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)", True, "live"),
    ]

    # Transient probe failure with healthy binary -> fallback catalog
    async def probe_transient(*args, timeout=5.0):
        if args[1] == "--version":
            return True, 0, "1.5.18", ""
        return False, 1, "", "read-only file system"

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_transient)
    fallback = await discover_provider_models("agy")
    assert fallback.status == "authenticated"
    assert fallback.discovery_status == "fallback"
    assert len(fallback.models) == len(AgyCliRunner.SUPPORTED_MODEL_SPECS)
    assert all(m.available and m.discovery_source == "fallback" for m in fallback.models)

    # Auth error -> unauthenticated + models marked unavailable
    async def probe_unauth(*args, timeout=5.0):
        if args[1] == "--version":
            return True, 0, "1.5.18", ""
        return False, 1, "", "Error: not logged in, please sign in"

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_unauth)
    unauth = await discover_provider_models("agy")
    assert unauth.status == "unauthenticated"
    assert unauth.discovery_status == "error"
    assert unauth.models and all(not m.available for m in unauth.models)


@pytest.mark.asyncio
async def test_discover_gemini_no_api_key_leak_and_live_json_plus_text_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.delenv("GOOGLE_GENAI_USE_GCA", raising=False)
    monkeypatch.delenv("GOOGLE_GENAI_USE_VERTEXAI", raising=False)

    # CRITICAL Invariant #1 check: GEMINI_API_KEY in env MUST NOT mark Gemini CLI as authenticated!
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-metered-secret")
    assert has_gemini_subscription_auth() is False

    # settings.json pointing at gemini-api-key MUST also return False
    (fake_home / ".gemini").mkdir()
    (fake_home / ".gemini" / "settings.json").write_text(
        json.dumps({"selectedType": "gemini-api-key"}), encoding="utf-8"
    )
    assert has_gemini_subscription_auth() is False

    # OAuth personal without oauth_creds.json -> still False
    (fake_home / ".gemini" / "settings.json").write_text(
        json.dumps({"security": {"auth": {"selectedType": "oauth-personal"}}}), encoding="utf-8"
    )
    assert has_gemini_subscription_auth() is False

    monkeypatch.setattr("dagger.providers.discovery.shutil.which", lambda cmd: f"/bin/{cmd}" if cmd == "gemini" else None)

    json_payload = json.dumps([
        {
            "id": "gemini-2.5-pro",
            "displayName": "gemini-2.5-pro",
            "contextLimits": {"inputTokens": 1048576, "outputTokens": 65536},
        },
        {
            "id": "gemini-3.5-flash",
            "displayName": "gemini-3.5-flash",
            "contextLimits": {"inputTokens": 1048576, "outputTokens": 65536},
        },
    ])

    async def probe_json(*args, timeout=5.0):
        if args[1:] == ("--version",):
            return True, 0, "0.70.0", ""
        if args[1:] == ("models", "-o", "json"):
            return True, 0, json_payload, ""
        return False, 1, "", ""

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_json)

    unauthed = await discover_provider_models("gemini")
    assert unauthed.status == "unauthenticated"
    assert [m.model_id for m in unauthed.models] == ["gemini-2.5-pro", "gemini-3.5-flash"]
    assert all(not m.available for m in unauthed.models)

    # Now create oauth_creds.json -> authenticated and models marked available=True!
    (fake_home / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
    assert has_gemini_subscription_auth() is True

    authed = await discover_provider_models("gemini")
    assert authed.status == "authenticated"
    assert authed.discovery_status == "ok"
    assert authed.models[0].context_window == 1_048_576
    assert all(m.available and m.discovery_source == "live" for m in authed.models)

    # Fallback to --list-models text format when models -o json fails
    text_output = (
        "Available Gemini Models:\n\n"
        "gemini-2.5-pro (gemini-2.5-pro)\n"
        "  Context limit: 1,048,576 input tokens / 65,536 output tokens\n\n"
        "gemini-3.1-flash-lite (gemini-3.1-flash-lite)\n"
        "  Context limit: 1,048,576 input tokens / 65,536 output tokens\n"
    )

    async def probe_text(*args, timeout=5.0):
        if args[1:] == ("--version",):
            return True, 0, "0.70.0", ""
        if args[1:] == ("models", "-o", "json"):
            return False, 1, "", "unsupported"
        if args[1:] == ("--list-models",):
            return True, 0, text_output, ""
        return False, 1, "", ""

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_text)
    from_text = await discover_provider_models("gemini")
    assert from_text.status == "authenticated"
    assert from_text.discovery_status == "ok"
    assert [(m.model_id, m.context_window) for m in from_text.models] == [
        ("gemini-2.5-pro", 1_048_576),
        ("gemini-3.1-flash-lite", 1_048_576),
    ]


@pytest.mark.asyncio
async def test_discover_copilot_missing_binary_guard_and_help_config_parsing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    # Even if gh is installed and logged in, missing copilot binary MUST return missing_binary!
    monkeypatch.setattr(
        "dagger.providers.discovery.shutil.which",
        lambda cmd: "/usr/bin/gh" if cmd == "gh" else None,
    )
    missing = await discover_provider_models("copilot")
    assert missing.status == "missing_binary"
    assert missing.models == []

    # Copilot binary present + gh auth status healthy + copilot help config output
    monkeypatch.setattr(
        "dagger.providers.discovery.shutil.which",
        lambda cmd: f"/usr/bin/{cmd}" if cmd in ("copilot", "gh") else None,
    )
    help_config_out = (
        "Configuration Settings:\n\n"
        "  `model`: AI model to use for Copilot CLI.\n"
        '    - "claude-sonnet-4.6"\n'
        '    - "gpt-5.4"\n'
        '    - "gpt-4.1"\n\n'
        "  `other`: something else\n"
    )

    async def fake_probe(*args, timeout=5.0):
        exe = Path(args[0]).name
        if exe == "copilot" and args[1:] == ("--version",):
            return True, 0, "copilot 1.0.0", ""
        if exe == "gh" and args[1:] == ("auth", "status"):
            return True, 0, "Logged in to github.com", ""
        if exe == "copilot" and args[1:] == ("help", "config"):
            return True, 0, help_config_out, ""
        return False, 1, "", ""

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", fake_probe)
    result = await discover_provider_models("copilot")
    assert result.status == "authenticated"
    assert result.discovery_status == "ok"
    assert [m.model_id for m in result.models] == ["claude-sonnet-4.6", "gpt-5.4", "gpt-4.1"]
    assert all(m.available and m.discovery_source == "live" for m in result.models)


@pytest.mark.asyncio
async def test_discover_claude_and_codex_curated_catalogs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("dagger.providers.discovery.shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    async def probe_ok(*args, timeout=5.0):
        if args[1:] == ("--version",):
            return True, 0, "2.1.0", ""
        return True, 0, '{"loggedIn": true}', ""

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_ok)
    claude = await discover_provider_models("claude")
    assert claude.status == "authenticated"
    assert [m.model_id for m in claude.models] == ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"]
    assert all(m.available and m.discovery_source == "curated" for m in claude.models)

    codex = await discover_provider_models("codex")
    assert codex.status == "authenticated"
    assert [m.model_id for m in codex.models] == [
        "gpt-5-codex",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
        "gpt-4.1",
        "gpt-4.1-mini",
    ]
    assert all(m.available and m.discovery_source == "curated" for m in codex.models)

    # Unauthenticated marks curated models available=False
    async def probe_logged_out(*args, timeout=5.0):
        if args[1:] == ("--version",):
            return True, 0, "2.1.0", ""
        return True, 0, '{"loggedIn": false}', ""

    monkeypatch.setattr("dagger.providers.discovery._run_cli_probe", probe_logged_out)
    claude_out = await discover_provider_models("claude")
    assert claude_out.status == "unauthenticated"
    assert claude_out.models and all(not m.available for m in claude_out.models)


@respx.mock
@pytest.mark.asyncio
async def test_discover_groq_live_401_and_transient_fallback():
    route = respx.get("https://api.groq.com/openai/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "llama-3.3-70b-versatile", "context_window": 131072},
                    {"id": "whisper-large-v3", "context_window": 0},
                    {"id": "playai-tts", "context_window": 0},
                ]
            },
        )
    )
    ok = await discover_provider_models("groq", api_key="gsk_valid")
    assert ok.status == "configured"
    assert ok.discovery_status == "ok"
    by_id = {m.model_id: m for m in ok.models}
    assert by_id["llama-3.3-70b-versatile"].is_llm is True
    assert by_id["llama-3.3-70b-versatile"].context_window == 131072
    assert by_id["whisper-large-v3"].is_llm is False
    assert by_id["playai-tts"].is_llm is False
    assert route.calls.last.request.headers["authorization"] == "Bearer gsk_valid"

    # 401 -> expired and empty models
    route.mock(return_value=httpx.Response(401, json={"error": "invalid_api_key"}))
    expired = await discover_provider_models("groq", api_key="gsk_bad")
    assert expired.status == "expired"
    assert expired.discovery_status == "error"
    assert expired.models == []

    # Transient ConnectError -> keeps key configured + fallback baseline
    route.mock(side_effect=httpx.ConnectError("network down"))
    fallback = await discover_provider_models("groq", api_key="gsk_valid")
    assert fallback.status == "configured"
    assert fallback.discovery_status == "fallback"
    assert any(m.model_id == "llama-3.3-70b-versatile" for m in fallback.models)


@respx.mock
@pytest.mark.asyncio
async def test_discover_custom_api_openai_anthropic_and_401():
    openai_route = respx.get("https://api.example.test/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
    )
    res_openai = await discover_provider_models(
        "api", base_url="https://api.example.test/v1", api_key="sk-test"
    )
    assert res_openai.status == "configured"
    assert res_openai.discovery_status == "ok"
    assert [m.model_id for m in res_openai.models] == ["gpt-4o-mini"]
    assert openai_route.calls.last.request.headers["authorization"] == "Bearer sk-test"

    anthropic_route = respx.get("https://api.anthropic.com/v1/models").mock(
        return_value=httpx.Response(
            200,
            json={"data": [{"id": "claude-sonnet-4-5", "display_name": "Claude Sonnet 4.5"}]},
        )
    )
    res_anth = await discover_provider_models(
        "api", base_url="https://api.anthropic.com", api_key="sk-ant-test"
    )
    assert res_anth.status == "configured"
    assert res_anth.discovery_status == "ok"
    assert [(m.model_id, m.display_name) for m in res_anth.models] == [
        ("claude-sonnet-4-5", "Claude Sonnet 4.5")
    ]
    assert anthropic_route.calls.last.request.headers["x-api-key"] == "sk-ant-test"
    assert anthropic_route.calls.last.request.headers["anthropic-version"] == "2023-06-01"

    openai_route.mock(return_value=httpx.Response(403, text="forbidden"))
    expired = await discover_provider_models(
        "api", base_url="https://api.example.test/v1", api_key="sk-bad"
    )
    assert expired.status == "expired"
    assert expired.models == []


@respx.mock
@pytest.mark.asyncio
async def test_discover_local_v1_models_ollama_tags_fallback_and_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    vault_file = tmp_path / "creds.env"
    monkeypatch.setenv("DAGGER_CREDENTIALS_FILE", str(vault_file))
    save_provider_credentials("api", "sk-metered-must-not-leak", base_url="https://api.example.test/v1")

    # OpenAI-compatible /v1/models
    v1_route = respx.get("http://127.0.0.1:11434/v1/models").mock(
        return_value=httpx.Response(200, json={"data": [{"id": "qwen2.5-coder:7b"}]})
    )
    res_v1 = await discover_provider_models("local", base_url="http://127.0.0.1:11434/v1")
    assert res_v1.status == "authenticated"
    assert res_v1.discovery_status == "ok"
    assert [m.model_id for m in res_v1.models] == ["qwen2.5-coder:7b"]
    # Must NOT leak DAGGER_API_KEY to local endpoint
    assert "authorization" not in v1_route.calls.last.request.headers

    # Ollama /api/tags fallback when /v1/models returns 404
    v1_route.mock(return_value=httpx.Response(404))
    respx.get("http://127.0.0.1:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "llama3.2:latest"}]})
    )
    res_ollama = await discover_provider_models("local", base_url="http://127.0.0.1:11434")
    assert res_ollama.status == "authenticated"
    assert res_ollama.discovery_status == "ok"
    assert [m.model_id for m in res_ollama.models] == ["llama3.2:latest"]

    # Unreachable local server
    v1_route.mock(side_effect=httpx.ConnectError("refused"))
    res_down = await discover_provider_models("local", base_url="http://127.0.0.1:11434")
    assert res_down.status == "unreachable"
    assert res_down.discovery_status == "error"
    assert res_down.models == []


@pytest.mark.asyncio
async def test_discover_tavily_is_non_llm_and_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch):
    def forbid_http(*args, **kwargs):
        raise AssertionError("Tavily discovery must never make network calls")

    monkeypatch.setattr(httpx, "AsyncClient", forbid_http)

    configured = await discover_provider_models("tavily", api_key="tvly-secret-123")
    assert configured.status == "configured"
    assert configured.discovery_status == "ok"
    assert len(configured.models) == 1
    assert configured.models[0].model_id == "tavily-search"
    assert configured.models[0].is_llm is False
    assert configured.models[0].available is True

    unconfigured = await discover_provider_models("tavily", api_key="")
    assert unconfigured.status == "unauthenticated"
    assert unconfigured.models[0].available is False
    assert unconfigured.models[0].is_llm is False


def test_parsers_handle_noisy_output_gracefully():
    agy_out = (
        "I1126 16:00:00 1 server.go:1] noisy log\n"
        "Available models:\n"
        "gemini-4-neon-high   Gemini 4 Neon (High)\n"
        "- Claude Sonnet 5.5 (Medium)\n"
    )
    agy_models = parse_agy_models_output(agy_out)
    assert [(m.model_id, m.display_name) for m in agy_models] == [
        ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
        ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
    ]

    gemini_json = parse_gemini_models_json('warning line\n[{"id":"gemini-2.5-pro","displayName":"Pro"}]')
    assert [m.model_id for m in gemini_json] == ["gemini-2.5-pro"]

    gemini_text = parse_gemini_models_text("gemini-3.5-flash (Flash)\n  Context limit: 1,000,000 input tokens\n")
    assert [(m.model_id, m.context_window) for m in gemini_text] == [("gemini-3.5-flash", 1_000_000)]

    copilot_models = parse_copilot_help_config_models('  `model`: desc\n    - "gpt-4.1"\n    - "gpt-5.2"\n')
    assert [m.model_id for m in copilot_models] == ["gpt-4.1", "gpt-5.2"]
