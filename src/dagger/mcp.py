"""Per-run Code Scalpel MCP injection and per-CLI tool scoping.

Workers run with the *target* project as their working directory, which usually has
no MCP configuration of its own. DAGGER therefore hands each provider CLI the pinned
Code Scalpel server definition on every run, together with that CLI's own
allow/deny syntax, so tool scoping is enforced by the CLI rather than by prompt text.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path

from .skills import MUTATING_CODESCALPEL_MCP_TOOLS, READ_ONLY_CODESCALPEL_MCP_TOOLS

CODESCALPEL_SERVER_NAME = "codescalpel"
# Keep in sync with the repo's .mcp.json (SkillForge-managed); a test enforces this.
CODESCALPEL_COMMAND = "uvx"
CODESCALPEL_ARGS: tuple[str, ...] = ("--with", "mcp==1.30.0", "codescalpel@2.1.4", "mcp")
CODESCALPEL_TOOL_PREFIX = f"mcp__{CODESCALPEL_SERVER_NAME}__"

# Abstract container tool names that grant write or execute access.
WRITE_TOOLS = frozenset({"write", "edit", "patch", "shell", "bash"})

_CLAUDE_BUILTINS: dict[str, tuple[str, ...]] = {
    "read": ("Read",),
    "search": ("Grep", "Glob"),
    "write": ("Write",),
    "edit": ("Edit",),
    "patch": ("Edit",),
    "shell": ("Bash",),
    "bash": ("Bash",),
    "web_search": ("WebSearch",),
}
_CLAUDE_WRITE_BUILTINS: tuple[str, ...] = ("Write", "Edit", "NotebookEdit", "Bash")
# Copilot needs no permission to read/search; only these kinds are grantable.
_COPILOT_BUILTINS: dict[str, str] = {"write": "write", "edit": "write", "patch": "write", "shell": "shell", "bash": "shell"}

_MUTATING_SUFFIXES = frozenset(t[len(CODESCALPEL_TOOL_PREFIX):] for t in MUTATING_CODESCALPEL_MCP_TOOLS)


def codescalpel_enabled() -> bool:
    """Code Scalpel is on by default; set DAGGER_CODESCALPEL=0 to disable injection."""
    return os.getenv("DAGGER_CODESCALPEL", "1").strip().lower() not in {"0", "false", "off", "no"}


def is_read_only_tools(allowed_tools: Iterable[str]) -> bool:
    tools = {tool.strip().casefold() for tool in allowed_tools if tool.strip()}
    return bool(tools) and tools.isdisjoint(WRITE_TOOLS)


def default_codescalpel_tools(read_only: bool) -> list[str]:
    """Tools every worker gets when Code Scalpel is enabled."""
    if read_only:
        return list(READ_ONLY_CODESCALPEL_MCP_TOOLS)
    return [*READ_ONLY_CODESCALPEL_MCP_TOOLS, *MUTATING_CODESCALPEL_MCP_TOOLS]


def tool_suffix(tool: str) -> str | None:
    """Return the bare Code Scalpel tool name for a canonical `mcp__codescalpel__x` name."""
    lowered = tool.strip().lower()
    if not lowered.startswith(CODESCALPEL_TOOL_PREFIX):
        return None
    suffix = lowered[len(CODESCALPEL_TOOL_PREFIX):]
    return suffix if suffix and suffix != "*" else None


def scope_codescalpel_tools(mcp_tools: Iterable[str], read_only: bool) -> list[str]:
    """Bare Code Scalpel tool names for this run; mutating tools are dropped for read-only containers.

    Runners re-apply this per container because a fallback container may be read-only even
    when the envelope was scoped for a write-capable primary.
    """
    known = {t[len(CODESCALPEL_TOOL_PREFIX):] for t in (*READ_ONLY_CODESCALPEL_MCP_TOOLS, *MUTATING_CODESCALPEL_MCP_TOOLS)}
    result: list[str] = []
    for tool in mcp_tools:
        suffix = tool_suffix(tool)
        if suffix is None or suffix not in known or suffix in result:
            continue
        if read_only and suffix in _MUTATING_SUFFIXES:
            continue
        result.append(suffix)
    return result


def mutating_suffixes() -> list[str]:
    return sorted(_MUTATING_SUFFIXES)


def _server(**extra: object) -> dict[str, object]:
    return {"command": CODESCALPEL_COMMAND, "args": list(CODESCALPEL_ARGS), **extra}


def claude_mcp_config() -> str:
    # Claude scopes individual tools via --allowedTools/--disallowedTools, not in the server config.
    return json.dumps({"mcpServers": {CODESCALPEL_SERVER_NAME: _server(type="stdio")}})


def copilot_mcp_config(suffixes: list[str]) -> str:
    return json.dumps({"mcpServers": {CODESCALPEL_SERVER_NAME: _server(type="local", tools=list(suffixes))}})


GEMINI_EXTENSION = "dagger-codescalpel"
GEMINI_READ_ONLY_EXTENSION = "dagger-codescalpel-readonly"


def gemini_extension_name(read_only: bool) -> str:
    return GEMINI_READ_ONLY_EXTENSION if read_only else GEMINI_EXTENSION


def gemini_extension_installed(name: str) -> bool:
    """True when the extension is linked/installed in the user's Gemini home."""
    home = Path(os.getenv("GEMINI_CLI_HOME") or Path.home())
    return (home / ".gemini" / "extensions" / name).exists()


def codex_config_overrides(suffixes: list[str]) -> list[str]:
    """`-c key=value` TOML overrides that define the server for one `codex exec` run."""
    key = f"mcp_servers.{CODESCALPEL_SERVER_NAME}"
    return [
        "-c", f"{key}.command={json.dumps(CODESCALPEL_COMMAND)}",
        "-c", f"{key}.args={json.dumps(list(CODESCALPEL_ARGS))}",
        "-c", f"{key}.enabled_tools={json.dumps(list(suffixes))}",
    ]


def claude_builtin_tools(allowed_tools: Iterable[str]) -> list[str]:
    """Map abstract container tool names to Claude Code tool names; unknown names pass through."""
    result: list[str] = []
    for tool in allowed_tools:
        name = tool.strip()
        if not name:
            continue
        for mapped in _CLAUDE_BUILTINS.get(name.casefold(), (name,)):
            if mapped not in result:
                result.append(mapped)
    return result


def claude_write_builtins() -> list[str]:
    return list(_CLAUDE_WRITE_BUILTINS)


def copilot_builtin_permissions(allowed_tools: Iterable[str]) -> list[str]:
    """Map abstract container tool names to Copilot permission kinds; read/search need none."""
    result: list[str] = []
    for tool in allowed_tools:
        name = tool.strip()
        lowered = name.casefold()
        if lowered in ("read", "search", "web_search", ""):
            continue
        mapped = _COPILOT_BUILTINS.get(lowered, name)
        if mapped not in result:
            result.append(mapped)
    return result
