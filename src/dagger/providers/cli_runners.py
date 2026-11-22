"""Subscription CLI runners, executed directly without a shell."""

import asyncio
import json
import os
import tempfile
from pathlib import Path

from .base import BaseProvider, ProviderError, ProviderRateLimitError, _RATE_MARKERS, raise_provider_error


class _CliRunner(BaseProvider):
    executable: str
    stripped_env: tuple[str, ...]

    def __init__(self, *, timeout: float = 120.0, max_concurrency: int | None = None) -> None:
        super().__init__(self.executable, max_concurrency=max_concurrency)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout

    def arguments(self, container, prompt: str, output: Path | None) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, output: Path | None) -> str:
        return stdout.strip()

    async def run(self, container, task) -> str:
        prompt = task.prompt
        if container.system_prompt:
            prompt = f"{container.system_prompt}\n\n{prompt}"
        async with self.semaphore(container.max_concurrency):
            output = None
            if self.executable == "codex":
                with tempfile.NamedTemporaryFile(
                    dir=Path.cwd(), prefix=".dagger-codex-", suffix=".txt", delete=False
                ) as temporary_output:
                    output = Path(temporary_output.name)
            try:
                args = self.arguments(container, prompt, output)
                env = os.environ.copy()
                if not (self.executable == "copilot" and container.use_token_auth):
                    for key in self.stripped_env:
                        env.pop(key, None)
                try:
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                        cwd=task.worktree_path or None,
                    )
                except OSError as exc:
                    raise ProviderError(f"Could not start {self.executable}: {exc}") from exc
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    await process.wait()
                    if isinstance(exc, asyncio.CancelledError):
                        raise
                    raise ProviderError(f"{self.executable} timed out after {self.timeout:g}s") from exc
                text = stdout.decode(errors="replace").strip()
                error = stderr.decode(errors="replace").strip()
                if any(marker in (text + "\n" + error).lower() for marker in _RATE_MARKERS):
                    raise ProviderRateLimitError(f"{self.executable} reported a rate limit: {error or text}")
                if process.returncode:
                    raise_provider_error(f"{self.executable} exited with status {process.returncode}: {error or text}")
                return self.parse(text, output)
            finally:
                if output is not None:
                    output.unlink(missing_ok=True)


def _json_result(text: str, field: str) -> str:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(data, dict):
        return text
    if data.get("is_error") or data.get("error"):
        raise_provider_error(str(data.get("error") or data.get("result")))
    result = data.get(field)
    if not isinstance(result, str):
        return text
    return result


class ClaudeCliRunner(_CliRunner):
    executable = "claude"
    stripped_env = ("ANTHROPIC_API_KEY",)

    def arguments(self, container, prompt, output):
        args = ["claude", "-p", prompt, "--output-format", "json"]
        if container.allowed_tools:
            args += ["--allowedTools", ",".join(container.allowed_tools)]
        return args

    def parse(self, stdout, output):
        return _json_result(stdout, "result")


class CodexCliRunner(_CliRunner):
    executable = "codex"
    stripped_env = ("OPENAI_API_KEY",)

    def arguments(self, container, prompt, output):
        return ["codex", "exec", "--sandbox", "workspace-write", "--output-last-message", str(output), prompt]

    def parse(self, stdout, output):
        try:
            result = output.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            result = ""
        return result or stdout


class GeminiCliRunner(_CliRunner):
    executable = "gemini"
    stripped_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def arguments(self, container, prompt, output):
        return ["gemini", "-p", prompt, "--output-format", "json"]

    def parse(self, stdout, output):
        return _json_result(stdout, "response")


class CopilotCliRunner(_CliRunner):
    executable = "copilot"
    stripped_env = ("GITHUB_TOKEN", "GH_TOKEN")

    def arguments(self, container, prompt, output):
        args = ["copilot", "-p", prompt, "--silent"]
        if container.allowed_tools:
            args += ["--allow-tool", ",".join(container.allowed_tools)]
        if container.model:
            args += ["--model", container.model]
        return args
