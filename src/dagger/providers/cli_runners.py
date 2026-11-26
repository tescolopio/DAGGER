"""Subscription CLI runners, executed directly without a shell."""

import asyncio
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from dagger.schema import UsageEvent

from .base import (
    BaseProvider,
    ProviderError,
    ProviderRateLimitError,
    _RATE_MARKERS,
    card_log_hook,
    estimate_cost_usd,
    estimate_tokens,
    raise_provider_error,
)

_METERED_ENV_KEYS = (
    "GROQ_API_KEY",
    "TAVILY_API_KEY",
    "DAGGER_API_KEY",
    "DAGGER_API_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)


class _CliRunner(BaseProvider):
    executable: str
    stripped_env: tuple[str, ...]

    def __init__(self, *, timeout: float = 120.0, max_concurrency: int | None = None, usage_callback=None) -> None:
        super().__init__(self.executable, max_concurrency=max_concurrency, usage_callback=usage_callback)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout

    def arguments(self, container, prompt: str, output: Path | None) -> list[str]:
        raise NotImplementedError

    def parse(self, stdout: str, output: Path | None) -> str:
        return stdout.strip()

    async def run(
        self, container, task, *, usage_context: dict[str, Any] | None = None
    ) -> str:
        prompt = task.prompt
        if container.system_prompt:
            prompt = f"{container.system_prompt}\n\n{prompt}"
        started = time.monotonic()
        success = False
        result_text = ""
        ctx = usage_context or {}
        try:
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
                    for key in _METERED_ENV_KEYS:
                        env.pop(key, None)
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
                        hook = card_log_hook.get()
                        if not (
                            isinstance(getattr(process, "stdout", None), asyncio.StreamReader)
                            or isinstance(getattr(process, "stderr", None), asyncio.StreamReader)
                        ):
                            stdout, stderr = await asyncio.wait_for(process.communicate(), self.timeout)
                        else:
                            stdout_chunks: list[bytes] = []
                            stderr_chunks: list[bytes] = []

                            async def _drain(
                                stream: asyncio.StreamReader | None,
                                channel: str,
                                sink: list[bytes],
                            ) -> None:
                                if stream is None:
                                    return
                                buf = b""
                                while True:
                                    chunk = await stream.read(4096)
                                    if not chunk:
                                        break
                                    sink.append(chunk)
                                    buf += chunk
                                    while b"\n" in buf:
                                        line_bytes, buf = buf.split(b"\n", 1)
                                        if hook is not None:
                                            decoded = line_bytes.decode(errors="replace").rstrip("\r")
                                            if decoded:
                                                try:
                                                    res = hook(channel, decoded)
                                                    if asyncio.iscoroutine(res):
                                                        await res
                                                except Exception:
                                                    pass
                                if buf and hook is not None:
                                    decoded = buf.decode(errors="replace").rstrip("\r")
                                    if decoded:
                                        try:
                                            res = hook(channel, decoded)
                                            if asyncio.iscoroutine(res):
                                                await res
                                        except Exception:
                                            pass

                            async def _run_and_drain() -> tuple[bytes, bytes]:
                                await asyncio.gather(
                                    _drain(process.stdout, "stdout", stdout_chunks),
                                    _drain(process.stderr, "stderr", stderr_chunks),
                                    process.wait(),
                                )
                                return b"".join(stdout_chunks), b"".join(stderr_chunks)

                            stdout, stderr = await asyncio.wait_for(_run_and_drain(), self.timeout)
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
                    result_text = self.parse(text, output)
                    success = True
                    return result_text
                finally:
                    if output is not None:
                        output.unlink(missing_ok=True)
        finally:
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            in_tok = estimate_tokens(prompt)
            out_tok = estimate_tokens(result_text)
            cost_tier = ctx.get("cost_tier") or getattr(container, "cost_tier", "flat_subscription")
            await self._emit_usage(
                UsageEvent(
                    run_id=ctx.get("run_id", getattr(task, "run_id", None)),
                    kanban_id=ctx.get("kanban_id", getattr(task, "kanban_id", None)),
                    container_id=ctx.get("container_id", getattr(container, "container_id", None)),
                    provider=self.executable,
                    model=getattr(container, "model", None),
                    cost_tier=cost_tier,
                    duration_ms=duration_ms,
                    input_tokens_est=in_tok,
                    output_tokens_est=out_tok,
                    estimated_cost_usd=estimate_cost_usd(cost_tier, in_tok, out_tok),
                    success=success,
                )
            )


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


def parse_swarm_line(line: str) -> dict[str, Any]:
    """Parse a blackboard stdout line into seq, author, tag, and message."""
    text = line.strip()
    seq_match = re.search(r"#(\d+)", text)
    seq: int | str | None = int(seq_match.group(1)) if seq_match else None
    tag = next((t for t in ("[done]", "[stall]", "[blocked]") if t in text), None)
    author = "agy"
    if ":" in text and not text.startswith("{"):
        prefix = text.split(":", 1)[0].strip()
        if prefix and len(prefix) <= 32:
            author = prefix.lstrip("#0123456789 ").strip() or "agy"
    return {"seq": seq, "author": author, "tag": tag, "message": text}


class AgyCliRunner(_CliRunner):
    """Headless Antigravity CLI runner with /swarm mode and live stdout streaming."""

    executable = "agy"
    stripped_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    SUPPORTED_MODELS: tuple[str, ...] = (
        "Gemini 4 Neon",
        "Gemini 4 Carbon",
        "Gemini 4 Argon",
        "Gemini 4 Hydrogen",
        "Orchestrator",
        "Gemini 3.8 Flash",
        "Claude Opus 5.5",
        "Claude Sonnet 5.5",
        "GPT-OSS 120B",
    )
    SUPPORTED_EFFORTS: tuple[str, ...] = (
        "low",
        "medium",
        "high",
        "xhigh",
        "neon",
        "carbon",
        "argon",
    )
    _DEFAULT_EFFORT_BY_MODEL: dict[str, str] = {
        "Gemini 4 Neon": "high",
        "Gemini 4 Carbon": "medium",
        "Gemini 4 Argon": "medium",
        "Gemini 4 Hydrogen": "low",
        "Orchestrator": "neon",
        "Gemini 3.8 Flash": "low",
        "Claude Opus 5.5": "high",
        "Claude Sonnet 5.5": "medium",
        "GPT-OSS 120B": "medium",
    }
    _SLUG_TO_MODEL: dict[str, str] = {
        "gemini-4-neon": "Gemini 4 Neon",
        "gemini-4-carbon": "Gemini 4 Carbon",
        "gemini-4-argon": "Gemini 4 Argon",
        "gemini-4-hydrogen": "Gemini 4 Hydrogen",
        "orchestrator": "Orchestrator",
        "gemini-3.8-flash": "Gemini 3.8 Flash",
        "gemini-3-8-flash": "Gemini 3.8 Flash",
        "claude-opus-5-5": "Claude Opus 5.5",
        "claude-opus-5.5": "Claude Opus 5.5",
        "claude-sonnet-5-5": "Claude Sonnet 5.5",
        "claude-sonnet-5.5": "Claude Sonnet 5.5",
        "gpt-oss-120b": "GPT-OSS 120B",
    }
    _FAST_TIER_KEYWORDS: tuple[str, ...] = (
        "haiku",
        "flash",
        "mini",
        "lite",
        "fast",
        "nano",
        "small",
        "hydrogen",
    )

    @classmethod
    def normalize_model_and_effort(
        cls, model: str | None, effort: str | None = None
    ) -> tuple[str, str]:
        """Normalize any model/effort string into exact supported `agy` CLI flags."""
        raw_model = (model or "").strip()
        parsed_effort: str | None = None

        if raw_model.endswith(")") and "(" in raw_model:
            base, _, paren = raw_model[:-1].rpartition("(")
            if base.strip():
                raw_model = base.strip()
                parsed_effort = paren.strip().lower()

        canonical_model: str | None = None
        for supported in cls.SUPPORTED_MODELS:
            if raw_model.lower() == supported.lower():
                canonical_model = supported
                break

        if canonical_model is None and raw_model:
            slug = raw_model.lower().replace("_", "-").replace(" ", "-")
            if slug in cls._SLUG_TO_MODEL:
                canonical_model = cls._SLUG_TO_MODEL[slug]
            else:
                for eff in cls.SUPPORTED_EFFORTS:
                    suffix = f"-{eff}"
                    if slug.endswith(suffix):
                        base_slug = slug[: -len(suffix)]
                        if base_slug in cls._SLUG_TO_MODEL:
                            canonical_model = cls._SLUG_TO_MODEL[base_slug]
                            if parsed_effort is None:
                                parsed_effort = eff
                            break

        if canonical_model is None:
            lowered = raw_model.lower()
            if any(kw in lowered for kw in cls._FAST_TIER_KEYWORDS):
                canonical_model = "Gemini 4 Hydrogen"
                if parsed_effort is None:
                    parsed_effort = "low"
            else:
                canonical_model = "Gemini 4 Neon"
                if parsed_effort is None:
                    parsed_effort = "high"

        candidate_effort = (effort or parsed_effort or "").strip().lower()
        if candidate_effort not in cls.SUPPORTED_EFFORTS:
            candidate_effort = cls._DEFAULT_EFFORT_BY_MODEL.get(canonical_model, "high")

        return canonical_model, candidate_effort

    def arguments(self, container, prompt, output):
        effective_prompt = prompt
        if container.container_id == "agy_swarm_squad" or container.role == "swarm_squad":
            effective_prompt = (
                "/swarm Execute non-interactively and strictly stay within allowed_write_paths.\n"
                + prompt
            )
        model_name, effort_val = self.normalize_model_and_effort(container.model)
        return ["agy", "-p", effective_prompt, "--model", model_name, "--effort", effort_val]

    def parse(self, stdout, output):
        return stdout.strip()

    async def run(self, container, task, usage_context: dict | None = None):
        on_swarm_post = (usage_context or {}).get("on_swarm_post")
        if on_swarm_post is None:
            return await super().run(container, task, usage_context=usage_context)
        prompt = task.prompt
        if container.system_prompt:
            prompt = f"{container.system_prompt}\n\n{prompt}"
        if (container.container_id == "agy_swarm_squad" or container.role == "swarm_squad") and getattr(
            task, "allowed_write_paths", None
        ):
            prompt = f"Allowed write paths: {list(task.allowed_write_paths)}\n\n{prompt}"
        args = self.arguments(container, prompt, None)
        stripped = set(self.stripped_env) | {
            "GROQ_API_KEY", "TAVILY_API_KEY", "DAGGER_API_KEY", "DAGGER_API_BASE_URL",
            "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
            "GITHUB_TOKEN", "GH_TOKEN",
        }
        env = {k: v for k, v in os.environ.items() if k not in stripped}
        started = time.monotonic()
        lines: list[str] = []
        stalled = False
        rc = 1
        stderr_bytes = b""
        output_text = ""
        success = False
        try:
            async with self.semaphore(container.max_concurrency):
                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=task.worktree_path or None,
                    env=env,
                )
                assert process.stdout is not None
                hook = card_log_hook.get()
                try:
                    async def _drain_agy_stdout() -> None:
                        nonlocal stalled
                        buf = b""
                        use_read = hasattr(process.stdout, "read")
                        while True:
                            chunk = (
                                await process.stdout.read(4096)
                                if use_read
                                else await process.stdout.readline()
                            )
                            if not chunk:
                                break
                            buf += chunk
                            while b"\n" in buf:
                                raw_line, buf = buf.split(b"\n", 1)
                                line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                                if line.strip():
                                    lines.append(line)
                                    parsed = parse_swarm_line(line)
                                    if parsed.get("tag") == "[stall]":
                                        stalled = True
                                    res = on_swarm_post(parsed)
                                    if asyncio.iscoroutine(res):
                                        await res
                                    if hook is not None:
                                        try:
                                            hres = hook("stdout", line)
                                            if asyncio.iscoroutine(hres):
                                                await hres
                                        except Exception:
                                            pass
                        if buf:
                            line = buf.decode("utf-8", errors="replace").rstrip("\r")
                            if line.strip():
                                lines.append(line)
                                parsed = parse_swarm_line(line)
                                if parsed.get("tag") == "[stall]":
                                    stalled = True
                                res = on_swarm_post(parsed)
                                if asyncio.iscoroutine(res):
                                    await res
                                if hook is not None:
                                    try:
                                        hres = hook("stdout", line)
                                        if asyncio.iscoroutine(hres):
                                            await hres
                                    except Exception:
                                        pass

                    async def _drain_agy_stderr() -> bytes:
                        if process.stderr is None:
                            return b""
                        if not isinstance(process.stderr, asyncio.StreamReader):
                            return await process.stderr.read()
                        chunks: list[bytes] = []
                        buf = b""
                        while True:
                            chunk = await process.stderr.read(4096)
                            if not chunk:
                                break
                            chunks.append(chunk)
                            buf += chunk
                            while b"\n" in buf:
                                raw_line, buf = buf.split(b"\n", 1)
                                line = raw_line.decode("utf-8", errors="replace").rstrip("\r")
                                if line and hook is not None:
                                    try:
                                        hres = hook("stderr", line)
                                        if asyncio.iscoroutine(hres):
                                            await hres
                                    except Exception:
                                        pass
                        if buf and hook is not None:
                            line = buf.decode("utf-8", errors="replace").rstrip("\r")
                            if line:
                                try:
                                    hres = hook("stderr", line)
                                    if asyncio.iscoroutine(hres):
                                        await hres
                                except Exception:
                                    pass
                        return b"".join(chunks)

                    async def _stream() -> tuple[bytes, int]:
                        _, stderr_b, rc_val = await asyncio.gather(
                            _drain_agy_stdout(),
                            _drain_agy_stderr(),
                            process.wait(),
                        )
                        return stderr_b, rc_val

                    stderr_bytes, rc = await asyncio.wait_for(_stream(), timeout=self.timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
            if stalled:
                raise ProviderError("agy inner swarm stalled")
            err = stderr_bytes.decode("utf-8", errors="replace").strip()
            combined = ("\n".join(lines) + "\n" + err).lower()
            if any(m in combined for m in _RATE_MARKERS):
                raise ProviderRateLimitError(err or "agy rate limit")
            if rc != 0:
                raise ProviderError(f"agy exited with status {rc}: {err}")
            output_text = self.parse("\n".join(lines), None)
            success = True
            return output_text
        finally:
            ctx = usage_context or {}
            in_tok = estimate_tokens(prompt)
            out_tok = estimate_tokens(output_text)
            await self._emit_usage(UsageEvent(
                run_id=ctx.get("run_id") or task.run_id,
                kanban_id=ctx.get("kanban_id") or task.kanban_id,
                container_id=ctx.get("container_id") or container.container_id,
                provider=container.provider,
                model=container.model or "default",
                cost_tier=ctx.get("cost_tier") or container.cost_tier,
                input_tokens_est=in_tok,
                output_tokens_est=out_tok,
                estimated_cost_usd=estimate_cost_usd(container.cost_tier, in_tok, out_tok),
                duration_ms=int((time.monotonic() - started) * 1000),
                success=success,
            ))
