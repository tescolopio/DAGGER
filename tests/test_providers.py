import asyncio
import json
import traceback
from pathlib import Path
from urllib.parse import urlunsplit

import httpx
import pytest
import respx

from dagger.providers import (
    ClaudeCliRunner,
    CodexCliRunner,
    CopilotCliRunner,
    GeminiCliRunner,
    OpenAICompatibleApiRunner,
    ProviderError,
    ProviderRateLimitError,
    ProviderRegistry,
)
from dagger.schema import AgentContainer, DefinitionOfDone, TaskEnvelope


@pytest.fixture
def agent():
    return AgentContainer(
        name="developer", role="developer", provider="claude", model="model",
        system_prompt="Be concise", allowed_tools=["read", "write"],
    )


@pytest.fixture
def task():
    return TaskEnvelope(
        task_id="task", run_id="run", kanban_id="board", container_id="agent",
        prompt="Explain", dod=DefinitionOfDone(acceptance_criteria=[]),
    )


def test_registry():
    registry = ProviderRegistry()
    assert all(registry.get(name) for name in ("claude", "codex", "gemini", "copilot", "api"))
    custom = CodexCliRunner()
    registry.register("CUSTOM", custom)
    assert registry.get("custom") is custom
    with pytest.raises(KeyError, match="Unknown provider"):
        registry.get("missing")


@pytest.mark.parametrize(
    ("runner", "expected", "stdout", "removed"),
    [
        (ClaudeCliRunner, ["claude", "-p", "Be concise\n\nExplain", "--output-format", "json", "--allowedTools", "read,write"], '{"result":"answer"}', ["ANTHROPIC_API_KEY"]),
        (GeminiCliRunner, ["gemini", "-p", "Be concise\n\nExplain", "--output-format", "json"], '{"response":"answer"}', ["GEMINI_API_KEY", "GOOGLE_API_KEY"]),
        (CopilotCliRunner, ["copilot", "-p", "Be concise\n\nExplain", "--silent", "--allow-tool", "read,write", "--model", "model"], "answer", ["GITHUB_TOKEN", "GH_TOKEN"]),
    ],
)
async def test_cli_commands_and_credentials(monkeypatch, agent, task, runner, expected, stdout, removed):
    seen = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return stdout.encode(), b""

    async def create(*args, **kwargs):
        seen["args"] = list(args)
        seen["env"] = kwargs["env"]
        return Process()

    for key in removed:
        monkeypatch.setenv(key, "example")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await runner().run(agent, task) == "answer"
    assert seen["args"] == expected
    assert all(key not in seen["env"] for key in removed)


async def test_copilot_token_auth_opt_in(monkeypatch, agent, task):
    agent.use_token_auth = True
    monkeypatch.setenv("GH_TOKEN", "example")

    class Process:
        returncode = 0

        async def communicate(self):
            return b"answer", b""

    async def create(*args, **kwargs):
        assert kwargs["env"]["GH_TOKEN"] == "example"
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await CopilotCliRunner().run(agent, task) == "answer"


async def test_codex_output_file_and_cleanup(monkeypatch, tmp_path, agent, task):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "example")
    paths = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"noise", b""

    async def create(*args, **kwargs):
        assert list(args[:5]) == ["codex", "exec", "--sandbox", "workspace-write", "--output-last-message"]
        assert args[6] == "Be concise\n\nExplain"
        assert "OPENAI_API_KEY" not in kwargs["env"]
        paths.append(Path(args[5]))
        assert paths[-1].parent == Path.cwd()
        assert paths[-1].stat().st_mode & 0o777 == 0o600
        paths[-1].write_text("answer\n")
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await CodexCliRunner().run(agent, task) == "answer"
    assert not paths[0].exists()


@pytest.mark.parametrize("message", ["429", "rate limit", "rate_limit", "quota exceeded", "usage limit", "too many requests"])
async def test_cli_rate_limit_markers(monkeypatch, agent, task, message):
    class Process:
        returncode = 1

        async def communicate(self):
            return b"", message.encode()

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(ProviderRateLimitError):
        await ClaudeCliRunner().run(agent, task)


@pytest.mark.parametrize(("stdout", "stderr"), [(b"rate_limit", b"unrelated"), (b"{}", b"usage limit")])
async def test_cli_rate_limit_with_success_exit(monkeypatch, agent, task, stdout, stderr):
    class Process:
        returncode = 0

        async def communicate(self):
            return stdout, stderr

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(ProviderRateLimitError):
        await ClaudeCliRunner().run(agent, task)


@pytest.mark.parametrize("stdout", ["plain text", '{"other":"value"}'])
async def test_claude_raw_fallback(monkeypatch, agent, task, stdout):
    class Process:
        returncode = 0

        async def communicate(self):
            return stdout.encode(), b""

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await ClaudeCliRunner().run(agent, task) == stdout


@pytest.mark.parametrize("remove_output", [False, True])
async def test_codex_empty_or_missing_output_fallback(monkeypatch, agent, task, remove_output):
    class Process:
        returncode = 0

        async def communicate(self):
            return b"fallback", b""

    async def create(*args, **kwargs):
        if remove_output:
            Path(args[5]).unlink()
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    assert await CodexCliRunner().run(agent, task) == "fallback"


async def test_cli_timeout_kills_and_reaps_process(monkeypatch, agent, task):
    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True

    process = Process()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    with pytest.raises(ProviderError, match="timed out"):
        await ClaudeCliRunner(timeout=0.01).run(agent, task)
    assert process.killed and process.waited


async def test_cli_cancellation_kills_and_reaps_process(monkeypatch, agent, task):
    started = asyncio.Event()

    class Process:
        returncode = None
        killed = False
        waited = False

        async def communicate(self):
            started.set()
            await asyncio.Event().wait()

        def kill(self):
            self.killed = True
            self.returncode = -9

        async def wait(self):
            self.waited = True

    process = Process()

    async def create(*args, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    execution = asyncio.create_task(ClaudeCliRunner().run(agent, task))
    await started.wait()
    execution.cancel()
    with pytest.raises(asyncio.CancelledError):
        await execution
    assert process.killed and process.waited


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_shared_provider_semaphore(monkeypatch, agent, task, limit):
    active = 0
    maximum = 0

    class Process:
        returncode = 0

        async def communicate(self):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.01)
            active -= 1
            return b'{"result":"ok"}', b""

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    agent.max_concurrency = limit
    runners = [ClaudeCliRunner(), ClaudeCliRunner()]
    await asyncio.gather(*(runners[i % 2].run(agent, task) for i in range(5)))
    assert maximum == limit


async def test_shared_provider_semaphore_respects_lower_active_limit(monkeypatch, agent, task):
    started = asyncio.Event()
    release = asyncio.Event()
    second_started = asyncio.Event()
    count = 0

    class Process:
        returncode = 0

        async def communicate(self):
            nonlocal count
            count += 1
            if count == 1:
                started.set()
                await release.wait()
            else:
                second_started.set()
            return b'{"result":"ok"}', b""

    async def create(*args, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    agent.max_concurrency = 1
    other = agent.model_copy(update={"max_concurrency": 3})
    first = asyncio.create_task(ClaudeCliRunner().run(agent, task))
    await started.wait()
    second = asyncio.create_task(ClaudeCliRunner().run(other, task))
    await asyncio.sleep(0.01)
    assert not second_started.is_set()
    release.set()
    await asyncio.gather(first, second)
    assert second_started.is_set()


@respx.mock
async def test_api_uses_configured_endpoint_and_key(monkeypatch, agent, task):
    agent.provider = "api"
    agent.base_url = "https://example.test/v1/"
    agent.api_key_env = "TEST_API_KEY"
    monkeypatch.setenv("TEST_API_KEY", "example")
    route = respx.post("https://example.test/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})
    )
    assert await OpenAICompatibleApiRunner().run(agent, task) == "answer"
    request = route.calls.last.request
    assert request.headers["authorization"] == "Bearer " + "example"
    assert json.loads(request.content) == {
        "model": "model",
        "messages": [
            {"role": "system", "content": "Be concise"},
            {"role": "user", "content": "Explain"},
        ],
    }


@respx.mock
async def test_api_credentials_only_for_api_provider_and_errors(monkeypatch, agent, task):
    agent.api_key_env = "TEST_API_KEY"
    monkeypatch.setenv("TEST_API_KEY", "example")
    route = respx.post("http://127.0.0.1:8080/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "local"}}]})
    )
    assert await OpenAICompatibleApiRunner().run(agent, task) == "local"
    assert "authorization" not in route.calls.last.request.headers
    route.mock(return_value=httpx.Response(429, text="limit"))
    with pytest.raises(ProviderRateLimitError):
        await OpenAICompatibleApiRunner().run(agent, task)
    route.mock(return_value=httpx.Response(200, json={"choices": []}))
    with pytest.raises(ProviderError, match="Invalid API provider response"):
        await OpenAICompatibleApiRunner().run(agent, task)


@respx.mock
@pytest.mark.parametrize("kind", ["status", "status-rate-limit", "json"])
async def test_api_does_not_expose_upstream_error_details(monkeypatch, agent, task, kind):
    agent.provider = "api"
    agent.api_key_env = "TEST_API_KEY"
    monkeypatch.setenv("TEST_API_KEY", "sensitive-value")
    url = urlunsplit(("https", "user:password" + "@example.test", "/", "token=secret", ""))
    if kind == "json":
        response = httpx.Response(200, json={"error": f"quota exceeded: sensitive-value {url}"})
    else:
        detail = "rate limit" if kind == "status-rate-limit" else "request failed"
        response = httpx.Response(400, text=f"{detail}: sensitive-value {url}")
    respx.post("http://127.0.0.1:8080/v1/chat/completions").mock(return_value=response)
    error_type = ProviderError if kind == "status" else ProviderRateLimitError
    with pytest.raises(error_type) as error:
        await OpenAICompatibleApiRunner().run(agent, task)
    rendered = "".join(traceback.format_exception(error.value))
    for secret in ("sensitive-value", "user", "password", "token=secret"):
        assert secret not in rendered


async def test_api_does_not_expose_httpx_error_details(monkeypatch, agent, task):
    agent.provider = "api"
    agent.api_key_env = "TEST_API_KEY"
    monkeypatch.setenv("TEST_API_KEY", "sensitive-value")

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, *args, **kwargs):
            url = urlunsplit(("https", "user:password" + "@example.test", "/", "token=secret", ""))
            raise httpx.ConnectError(f"request failed: sensitive-value {url}")

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    with pytest.raises(ProviderError, match="ConnectError") as error:
        await OpenAICompatibleApiRunner().run(agent, task)
    rendered = "".join(traceback.format_exception(error.value))
    for secret in ("sensitive-value", "user", "password", "token=secret"):
        assert secret not in rendered


@pytest.mark.parametrize(
    "url",
    [
        urlunsplit(("https", "user:password" + "@example.test", "/", "", "")),
        "https://@example.test/v1",
        "file:///etc/secret",
        "http://[",
    ],
)
async def test_api_rejects_invalid_or_credential_bearing_base_urls(agent, task, url):
    agent.base_url = url
    with pytest.raises(ProviderError, match="without embedded credentials") as error:
        await OpenAICompatibleApiRunner().run(agent, task)
    assert url not in str(error.value)
