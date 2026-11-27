"""Subscription CLI runners, executed directly without a shell."""

import asyncio
import inspect
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

from dagger import mcp
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
    "COPILOT_GITHUB_TOKEN",
    "COPILOT_PROVIDER_API_KEY",
    "COPILOT_PROVIDER_BEARER_TOKEN",
    "COPILOT_PROVIDER_BASE_URL",
)


def _accepts_mcp_tools(method) -> bool:
    try:
        params = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return False
    return "mcp_tools" in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


class _CliRunner(BaseProvider):
    executable: str
    stripped_env: tuple[str, ...]

    def __init__(self, *, timeout: float = 120.0, max_concurrency: int | None = None, usage_callback=None) -> None:
        super().__init__(self.executable, max_concurrency=max_concurrency, usage_callback=usage_callback)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout

    def arguments(
        self, container, prompt: str, output: Path | None, mcp_tools: list[str] | None = None
    ) -> list[str]:
        raise NotImplementedError

    @staticmethod
    def scoped_mcp(container, mcp_tools: list[str] | None) -> tuple[list[str], bool]:
        """Bare Code Scalpel tool names allowed for this container, and whether it is read-only."""
        read_only = mcp.is_read_only_tools(container.allowed_tools)
        return mcp.scope_codescalpel_tools(mcp_tools or [], read_only), read_only

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
                mcp_tools = list(getattr(task, "allowed_mcp_tools", None) or [])
                if self.executable == "codex":
                    with tempfile.NamedTemporaryFile(
                        dir=Path.cwd(), prefix=".dagger-codex-", suffix=".txt", delete=False
                    ) as temporary_output:
                        output = Path(temporary_output.name)
                try:
                    if _accepts_mcp_tools(self.arguments):
                        args = self.arguments(container, prompt, output, mcp_tools=mcp_tools)
                    else:  # subclasses written before per-run MCP scoping
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

    def arguments(self, container, prompt, output, mcp_tools=None):
        args = ["claude", "-p", prompt, "--output-format", "json"]
        suffixes, read_only = self.scoped_mcp(container, mcp_tools)
        allowed = mcp.claude_builtin_tools(container.allowed_tools)
        allowed += [mcp.CODESCALPEL_TOOL_PREFIX + s for s in suffixes]
        if suffixes:
            # Strict: the worker sees only DAGGER's Code Scalpel server, not the user's other MCP servers.
            args += ["--mcp-config", mcp.claude_mcp_config(), "--strict-mcp-config"]
        if allowed:
            args += ["--allowedTools", ",".join(allowed)]
        if read_only:
            denied = mcp.claude_write_builtins()
            if suffixes:
                denied += [mcp.CODESCALPEL_TOOL_PREFIX + s for s in mcp.mutating_suffixes()]
            args += ["--disallowedTools", ",".join(denied)]
        return args

    def parse(self, stdout, output):
        return _json_result(stdout, "result")


class CodexCliRunner(_CliRunner):
    executable = "codex"
    stripped_env = ("OPENAI_API_KEY",)

    def arguments(self, container, prompt, output, mcp_tools=None):
        suffixes, read_only = self.scoped_mcp(container, mcp_tools)
        args = ["codex", "exec", "--sandbox", "read-only" if read_only else "workspace-write"]
        if suffixes:
            # Unverified locally (codex not installed): relies on `-c mcp_servers.<name>.enabled_tools`.
            args += mcp.codex_config_overrides(suffixes)
        return args + ["--output-last-message", str(output), prompt]

    def parse(self, stdout, output):
        try:
            result = output.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            result = ""
        return result or stdout


class GeminiCliRunner(_CliRunner):
    executable = "gemini"
    stripped_env = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def arguments(self, container, prompt, output, mcp_tools=None):
        args = ["gemini", "-p", prompt, "--output-format", "json"]
        suffixes, read_only = self.scoped_mcp(container, mcp_tools)
        # Gemini has no per-run MCP flag (and ignores non-root-owned system settings), so the
        # server comes from a DAGGER extension linked once by scripts/setup_codescalpel_clis.sh.
        # The read-only variant excludes the mutating tools at the Gemini level.
        extension = mcp.gemini_extension_name(read_only)
        if suffixes and mcp.gemini_extension_installed(extension):
            args += ["-e", extension, "--allowed-mcp-server-names", mcp.CODESCALPEL_SERVER_NAME]
        if read_only:
            args += ["--approval-mode", "plan"]
        return args

    def parse(self, stdout, output):
        return _json_result(stdout, "response")


class CopilotCliRunner(_CliRunner):
    executable = "copilot"
    stripped_env = ("GITHUB_TOKEN", "GH_TOKEN")

    def arguments(self, container, prompt, output, mcp_tools=None):
        args = ["copilot", "-p", prompt, "--silent"]
        suffixes, read_only = self.scoped_mcp(container, mcp_tools)
        server = mcp.CODESCALPEL_SERVER_NAME
        if suffixes:
            args += ["--additional-mcp-config", mcp.copilot_mcp_config(suffixes)]
        for permission in mcp.copilot_builtin_permissions(container.allowed_tools):
            args += ["--allow-tool", permission]
        for suffix in suffixes:
            args += ["--allow-tool", f"{server}({suffix})"]
        if read_only:
            # Deny rules take precedence over allow rules in Copilot.
            for denied in ("write", "shell", *(f"{server}({s})" for s in mcp.mutating_suffixes())):
                args += ["--deny-tool", denied]
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

    # Exact (slug, display_label) catalog returned by `agy models` (v1.5.18).
    # Every model bakes its reasoning tier into the model ID itself; passing
    # `--effort` causes `agy` to fail closed with `--effort is not supported`.
    SUPPORTED_MODEL_SPECS: tuple[tuple[str, str], ...] = (
        ("gemini-4-neon-high", "Gemini 4 Neon (High)"),
        ("gemini-4-neon-xhigh", "Gemini 4 Neon (XHigh)"),
        ("gemini-4-neon-medium", "Gemini 4 Neon (Medium)"),
        ("gemini-4-neon-low", "Gemini 4 Neon (Low)"),
        ("gemini-4-carbon-medium", "Gemini 4 Carbon (Medium)"),
        ("gemini-4-carbon-high", "Gemini 4 Carbon (High)"),
        ("gemini-4-carbon-low", "Gemini 4 Carbon (Low)"),
        ("gemini-4-argon-medium", "Gemini 4 Argon (Medium)"),
        ("gemini-4-argon-high", "Gemini 4 Argon (High)"),
        ("gemini-4-argon-low", "Gemini 4 Argon (Low)"),
        ("gemini-4-hydrogen-high", "Gemini 4 Hydrogen (High)"),
        ("gemini-4-hydrogen-xhigh", "Gemini 4 Hydrogen (XHigh)"),
        ("gemini-4-hydrogen-medium", "Gemini 4 Hydrogen (Medium)"),
        ("gemini-4-hydrogen-low", "Gemini 4 Hydrogen (Low)"),
        ("gemini-pro-orchestrator-neon", "Orchestrator (Neon)"),
        ("gemini-pro-orchestrator", "Orchestrator (Carbon)"),
        ("gemini-pro-orchestrator-argon", "Orchestrator (Argon)"),
        ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
        ("gemini-3.8-flash-medium", "Gemini 3.8 Flash (Medium)"),
        ("gemini-3.8-flash-low", "Gemini 3.8 Flash (Low)"),
        ("claude-opus-5-5-low", "Claude Opus 5.5 (Low)"),
        ("claude-opus-5-5-medium", "Claude Opus 5.5 (Medium)"),
        ("claude-opus-5-5-high", "Claude Opus 5.5 (High)"),
        ("claude-sonnet-5-5-low", "Claude Sonnet 5.5 (Low)"),
        ("claude-sonnet-5-5-medium", "Claude Sonnet 5.5 (Medium)"),
        ("claude-sonnet-5-5-high", "Claude Sonnet 5.5 (High)"),
        ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
    )
    SUPPORTED_MODELS: tuple[str, ...] = tuple(slug for slug, _ in SUPPORTED_MODEL_SPECS)
    SUPPORTED_LABELS: tuple[str, ...] = tuple(label for _, label in SUPPORTED_MODEL_SPECS)
    _SLUG_MAP: dict[str, str] = {
        **{slug.lower(): slug for slug, _ in SUPPORTED_MODEL_SPECS},
        # Punctuation aliases (e.g. `gemini-3-8-flash-low`, `claude-opus-5.5-high`).
        "gemini-3-8-flash-high": "gemini-3.8-flash-high",
        "gemini-3-8-flash-medium": "gemini-3.8-flash-medium",
        "gemini-3-8-flash-low": "gemini-3.8-flash-low",
        "claude-opus-5.5-low": "claude-opus-5-5-low",
        "claude-opus-5.5-medium": "claude-opus-5-5-medium",
        "claude-opus-5.5-high": "claude-opus-5-5-high",
        "claude-sonnet-5.5-low": "claude-sonnet-5-5-low",
        "claude-sonnet-5.5-medium": "claude-sonnet-5-5-medium",
        "claude-sonnet-5.5-high": "claude-sonnet-5-5-high",
        "orchestrator-neon": "gemini-pro-orchestrator-neon",
        "orchestrator-carbon": "gemini-pro-orchestrator",
        "orchestrator-argon": "gemini-pro-orchestrator-argon",
        "gemini-pro-orchestrator-carbon": "gemini-pro-orchestrator",
    }
    _LABEL_TO_SLUG: dict[str, str] = {label.lower(): slug for slug, label in SUPPORTED_MODEL_SPECS}
    _FAMILY_TIERS: dict[str, dict[str, str]] = {
        "gemini-4-neon": {
            "high": "gemini-4-neon-high",
            "xhigh": "gemini-4-neon-xhigh",
            "medium": "gemini-4-neon-medium",
            "low": "gemini-4-neon-low",
        },
        "gemini-4-carbon": {
            "medium": "gemini-4-carbon-medium",
            "high": "gemini-4-carbon-high",
            "low": "gemini-4-carbon-low",
        },
        "gemini-4-argon": {
            "medium": "gemini-4-argon-medium",
            "high": "gemini-4-argon-high",
            "low": "gemini-4-argon-low",
        },
        "gemini-4-hydrogen": {
            "high": "gemini-4-hydrogen-high",
            "xhigh": "gemini-4-hydrogen-xhigh",
            "medium": "gemini-4-hydrogen-medium",
            "low": "gemini-4-hydrogen-low",
        },
        "orchestrator": {
            "neon": "gemini-pro-orchestrator-neon",
            "carbon": "gemini-pro-orchestrator",
            "argon": "gemini-pro-orchestrator-argon",
        },
        "gemini-3.8-flash": {
            "high": "gemini-3.8-flash-high",
            "medium": "gemini-3.8-flash-medium",
            "low": "gemini-3.8-flash-low",
        },
        "claude-opus-5-5": {
            "low": "claude-opus-5-5-low",
            "medium": "claude-opus-5-5-medium",
            "high": "claude-opus-5-5-high",
        },
        "claude-sonnet-5-5": {
            "low": "claude-sonnet-5-5-low",
            "medium": "claude-sonnet-5-5-medium",
            "high": "claude-sonnet-5-5-high",
        },
        "gpt-oss-120b": {
            "medium": "gpt-oss-120b-medium",
        },
    }
    _DEFAULT_TIER_BY_FAMILY: dict[str, str] = {
        "gemini-4-neon": "high",
        "gemini-4-carbon": "medium",
        "gemini-4-argon": "medium",
        "gemini-4-hydrogen": "low",
        "orchestrator": "neon",
        "gemini-3.8-flash": "low",
        "claude-opus-5-5": "high",
        "claude-sonnet-5-5": "medium",
        "gpt-oss-120b": "medium",
    }
    _FAMILY_ALIASES: dict[str, str] = {
        "gemini-4-neon": "gemini-4-neon",
        "gemini-4-carbon": "gemini-4-carbon",
        "gemini-4-argon": "gemini-4-argon",
        "gemini-4-hydrogen": "gemini-4-hydrogen",
        "orchestrator": "orchestrator",
        "gemini-3.8-flash": "gemini-3.8-flash",
        "gemini-3-8-flash": "gemini-3.8-flash",
        "claude-opus-5-5": "claude-opus-5-5",
        "claude-opus-5.5": "claude-opus-5-5",
        "claude-sonnet-5-5": "claude-sonnet-5-5",
        "claude-sonnet-5.5": "claude-sonnet-5-5",
        "gpt-oss-120b": "gpt-oss-120b",
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
    def normalize_model(cls, model: str | None, effort: str | None = None) -> str:
        """Normalize any model/tier string into an exact valid `agy models` slug."""
        raw_model = (model or "").strip()
        explicit_tier = (effort or "").strip().lower() or None

        # Exact display label or slug match when no overriding effort was requested.
        if raw_model and explicit_tier is None:
            lowered_raw = raw_model.lower()
            if lowered_raw in cls._LABEL_TO_SLUG:
                return cls._LABEL_TO_SLUG[lowered_raw]
            direct_slug = lowered_raw.replace("_", "-").replace(" ", "-")
            if direct_slug in cls._SLUG_MAP:
                return cls._SLUG_MAP[direct_slug]

        parsed_tier: str | None = None
        base_text = raw_model
        if base_text.endswith(")") and "(" in base_text:
            base, _, paren = base_text[:-1].rpartition("(")
            if base.strip():
                base_text = base.strip()
                parsed_tier = paren.strip().lower() or None

        slug = base_text.lower().replace("_", "-").replace(" ", "-")
        # If slug already names a specific catalog entry, resolve its family and tier
        # so an explicit `effort` override can still select a sibling tier cleanly.
        if slug in cls._SLUG_MAP:
            resolved_slug = cls._SLUG_MAP[slug]
            for family, tiers in cls._FAMILY_TIERS.items():
                for tier_name, tier_slug in tiers.items():
                    if tier_slug == resolved_slug:
                        chosen = explicit_tier or parsed_tier or tier_name
                        if chosen in tiers:
                            return tiers[chosen]
                        return resolved_slug

        family = cls._FAMILY_ALIASES.get(slug)
        if family is None and slug:
            for candidate_family, tiers in cls._FAMILY_TIERS.items():
                for alias, mapped in cls._FAMILY_ALIASES.items():
                    if mapped != candidate_family:
                        continue
                    for tier_name in tiers:
                        if slug == f"{alias}-{tier_name}":
                            family = candidate_family
                            if parsed_tier is None:
                                parsed_tier = tier_name
                            break
                    if family is not None:
                        break
                if family is not None:
                    break

        if family is None:
            lowered = raw_model.lower()
            if any(kw in lowered for kw in cls._FAST_TIER_KEYWORDS):
                family = "gemini-4-hydrogen"
            else:
                family = "gemini-4-neon"

        tiers = cls._FAMILY_TIERS[family]
        candidate_tier = explicit_tier or parsed_tier or cls._DEFAULT_TIER_BY_FAMILY[family]
        if candidate_tier in tiers:
            return tiers[candidate_tier]
        return tiers[cls._DEFAULT_TIER_BY_FAMILY[family]]

    @classmethod
    def normalize_model_and_effort(
        cls, model: str | None, effort: str | None = None
    ) -> tuple[str, None]:
        """Return `(canonical_agy_model_slug, None)`.

        `agy` v1.5.18 encodes reasoning effort inside each model identifier and
        rejects `--effort` for all catalog models, so effort is always `None`.
        """
        return cls.normalize_model(model, effort=effort), None

    def arguments(self, container, prompt, output, mcp_tools=None):
        is_swarm = container.container_id == "agy_swarm_squad" or container.role == "swarm_squad"
        effective_prompt = prompt
        if is_swarm:
            effective_prompt = (
                "/swarm Execute non-interactively and strictly stay within allowed_write_paths.\n"
                + prompt
            )
        model_id = self.normalize_model(container.model)
        args = ["agy", "-p", effective_prompt, "--model", model_id]
        if not is_swarm:
            args += ["--disable-slash-commands"]
        # agy has no per-run MCP or per-tool flags: Code Scalpel comes from the user-level
        # `agy mcp add codescalpel ...` registration, and plan mode is the read-only enforcement.
        if mcp.is_read_only_tools(container.allowed_tools):
            args += ["--mode", "plan"]
        return args

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
