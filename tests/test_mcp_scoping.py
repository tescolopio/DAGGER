"""Per-run Code Scalpel MCP injection and CLI-level tool scoping."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from dagger import mcp
from dagger.agents.orchestrator import Orchestrator
from dagger.agents.project_manager import ProjectManager
from dagger.db import DatabaseManager
from dagger.providers.cli_runners import (
    AgyCliRunner,
    ClaudeCliRunner,
    CodexCliRunner,
    CopilotCliRunner,
    GeminiCliRunner,
)
from dagger.schema import AgentContainer, KanbanTask, MilestoneTask
from dagger.skills import MUTATING_CODESCALPEL_MCP_TOOLS, READ_ONLY_CODESCALPEL_MCP_TOOLS

ALL_TOOLS = [*READ_ONLY_CODESCALPEL_MCP_TOOLS, *MUTATING_CODESCALPEL_MCP_TOOLS]


def _container(provider: str, tools: list[str]) -> AgentContainer:
    return AgentContainer(name="c", role="worker", provider=provider, model="m", allowed_tools=tools)


def _flag_values(args: list[str], flag: str) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args) if arg == flag]


def test_pinned_server_matches_repo_mcp_json():
    repo_cfg = json.loads((Path(__file__).resolve().parents[1] / ".mcp.json").read_text())
    server = repo_cfg["mcpServers"][mcp.CODESCALPEL_SERVER_NAME]
    assert server["command"] == mcp.CODESCALPEL_COMMAND
    assert tuple(server["args"]) == mcp.CODESCALPEL_ARGS
    # The Gemini extensions and the agy setup script carry the same pin.
    root = Path(__file__).resolve().parents[1]
    for manifest in (root / "integrations" / "gemini").glob("*/gemini-extension.json"):
        assert tuple(json.loads(manifest.read_text())["mcpServers"]["codescalpel"]["args"]) == mcp.CODESCALPEL_ARGS
    script = (root / "scripts" / "setup_codescalpel_clis.sh").read_text()
    assert f"CODESCALPEL_ARGS=({' '.join(mcp.CODESCALPEL_ARGS)})" in script


def test_scope_drops_mutating_tools_for_read_only_and_ignores_unknown():
    tools = [*ALL_TOOLS, "mcp__codescalpel__*", "mcp__other__thing", "mcp__codescalpel__delete_symbol"]
    read_only = mcp.scope_codescalpel_tools(tools, read_only=True)
    assert "update_symbol" not in read_only and "rename_symbol" not in read_only
    assert "extract_code" in read_only and "simulate_refactor" in read_only
    assert "delete_symbol" not in read_only and "*" not in read_only
    assert set(mcp.scope_codescalpel_tools(tools, read_only=False)) >= {"update_symbol", "rename_symbol"}


def test_claude_read_only_injects_server_and_denies_mutation():
    args = ClaudeCliRunner().arguments(_container("claude", ["read", "search"]), "p", None, mcp_tools=ALL_TOOLS)
    config = json.loads(_flag_values(args, "--mcp-config")[0])
    assert config["mcpServers"]["codescalpel"]["args"] == list(mcp.CODESCALPEL_ARGS)
    assert "--strict-mcp-config" in args
    allowed = _flag_values(args, "--allowedTools")[0].split(",")
    assert allowed[:3] == ["Read", "Grep", "Glob"]
    assert "mcp__codescalpel__extract_code" in allowed
    assert "mcp__codescalpel__update_symbol" not in allowed
    denied = _flag_values(args, "--disallowedTools")[0].split(",")
    assert {"Write", "Edit", "Bash", "mcp__codescalpel__update_symbol", "mcp__codescalpel__rename_symbol"} <= set(denied)


def test_claude_writer_gets_refactor_tools_and_no_deny_list():
    args = ClaudeCliRunner().arguments(_container("claude", ["read", "write", "shell"]), "p", None, mcp_tools=ALL_TOOLS)
    allowed = _flag_values(args, "--allowedTools")[0].split(",")
    assert {"Write", "Bash", "mcp__codescalpel__update_symbol"} <= set(allowed)
    assert "--disallowedTools" not in args


def test_claude_without_mcp_tools_leaves_mcp_config_alone():
    args = ClaudeCliRunner().arguments(_container("claude", ["read", "write"]), "p", None, mcp_tools=[])
    assert "--mcp-config" not in args and "--strict-mcp-config" not in args


def test_copilot_uses_server_patterns_and_deny_rules():
    args = CopilotCliRunner().arguments(_container("copilot", ["read", "search"]), "p", None, mcp_tools=ALL_TOOLS)
    config = json.loads(_flag_values(args, "--additional-mcp-config")[0])
    assert "update_symbol" not in config["mcpServers"]["codescalpel"]["tools"]
    allowed = _flag_values(args, "--allow-tool")
    assert "codescalpel(extract_code)" in allowed and "read" not in allowed
    denied = set(_flag_values(args, "--deny-tool"))
    assert {"write", "shell", "codescalpel(update_symbol)", "codescalpel(rename_symbol)"} <= denied


def test_codex_read_only_sandbox_and_config_overrides():
    output = Path("out.txt")
    ro = CodexCliRunner().arguments(_container("codex", ["read", "search"]), "p", output, mcp_tools=ALL_TOOLS)
    assert ro[ro.index("--sandbox") + 1] == "read-only"
    overrides = _flag_values(ro, "-c")
    assert 'mcp_servers.codescalpel.command="uvx"' in overrides
    enabled = next(o for o in overrides if o.startswith("mcp_servers.codescalpel.enabled_tools="))
    assert "update_symbol" not in enabled
    assert ro[-1] == "p"
    rw = CodexCliRunner().arguments(_container("codex", ["read", "write"]), "p", output, mcp_tools=[])
    assert rw[rw.index("--sandbox") + 1] == "workspace-write" and "-c" not in rw


def test_gemini_selects_linked_extension_and_plan_mode(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path))
    runner = GeminiCliRunner()
    ro_container = _container("gemini", ["read", "search"])
    # Not linked yet: worker still runs, just without Code Scalpel.
    args = runner.arguments(ro_container, "p", None, mcp_tools=ALL_TOOLS)
    assert "-e" not in args and _flag_values(args, "--approval-mode") == ["plan"]

    for name in (mcp.GEMINI_EXTENSION, mcp.GEMINI_READ_ONLY_EXTENSION):
        (tmp_path / ".gemini" / "extensions" / name).mkdir(parents=True)
    args = runner.arguments(ro_container, "p", None, mcp_tools=ALL_TOOLS)
    assert _flag_values(args, "-e") == [mcp.GEMINI_READ_ONLY_EXTENSION]
    assert _flag_values(args, "--allowed-mcp-server-names") == ["codescalpel"]
    rw = runner.arguments(_container("gemini", ["read", "write"]), "p", None, mcp_tools=ALL_TOOLS)
    assert _flag_values(rw, "-e") == [mcp.GEMINI_EXTENSION] and "--approval-mode" not in rw


def test_gemini_extension_manifests_scope_tools():
    root = Path(__file__).resolve().parents[1] / "integrations" / "gemini"
    ro = json.loads((root / mcp.GEMINI_READ_ONLY_EXTENSION / "gemini-extension.json").read_text())
    rw = json.loads((root / mcp.GEMINI_EXTENSION / "gemini-extension.json").read_text())
    assert set(ro["mcpServers"]["codescalpel"]["excludeTools"]) == set(mcp.mutating_suffixes())
    assert "excludeTools" not in rw["mcpServers"]["codescalpel"]


def test_agy_read_only_uses_plan_mode():
    ro = AgyCliRunner().arguments(_container("agy", ["read", "search"]), "p", None, mcp_tools=ALL_TOOLS)
    assert _flag_values(ro, "--mode") == ["plan"]
    rw = AgyCliRunner().arguments(_container("agy", ["read", "write"]), "p", None)
    assert "--mode" not in rw


@pytest.mark.asyncio
@pytest.mark.parametrize("disabled", [False, True])
async def test_orchestrator_grants_code_scalpel_by_default(tmp_path: Path, monkeypatch, disabled: bool):
    if disabled:
        monkeypatch.setenv("DAGGER_CODESCALPEL", "0")
    else:
        monkeypatch.delenv("DAGGER_CODESCALPEL", raising=False)
    async with DatabaseManager(tmp_path / "db.sqlite3") as db:
        for existing in await db.list_containers():
            await db.upsert_container(existing.model_copy(update={"active": False}))
        await db.upsert_container(AgentContainer(
            container_id="writer", name="writer", role="worker", provider="claude", model="m",
            allowed_tools=["read", "write"], strengths=["coding"],
        ))
        await db.upsert_task(KanbanTask(task_id="t1", title="Build", description="Build it"))
        orch = Orchestrator(db=db, registry=SimpleNamespace(), project_manager=ProjectManager(workspace=tmp_path),
                            worktree_path=tmp_path, bus_path=tmp_path / "bus")
        envelopes = []

        async def fake_run_provider(container, envelope):
            envelopes.append(envelope)
            return "done"

        orch._run_provider = fake_run_provider  # type: ignore[method-assign]
        milestone = MilestoneTask(
            milestone_id="m1", title="Add parser", description="Write a parser module",
            required_skills=[],
            dod={"acceptance_criteria": ["ok"], "expected_artifacts": [], "verification_commands": ["true"]},
        )
        await orch._work({"kanban_id": "t1", "run_id": "r1",
                          "task": {**milestone.model_dump(mode="json"), "_hop_count": 0}})

    tools = envelopes[0].allowed_mcp_tools
    prompt = json.loads(envelopes[0].prompt)
    if disabled:
        assert tools == [] and "code_scalpel" not in prompt
    else:
        assert set(ALL_TOOLS) <= set(tools)
        assert "update_symbol" in prompt["code_scalpel"]
