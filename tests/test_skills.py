"""Tests for SkillLoader, SkillSummary, /api/skills, and Orchestrator/PM integration."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from dagger.agents.orchestrator import Orchestrator
from dagger.agents.project_manager import ProjectManager
from dagger.db import DatabaseManager
from dagger.schema import (
    AgentContainer,
    KanbanTask,
    MilestoneTask,
)
from dagger.server import create_app
from dagger.skills import (
    discover_skills,
    format_matched_skills_for_prompt,
    format_skills_for_planner,
    match_skills,
)


def _write_skill(base: Path, name: str, content: str) -> Path:
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(content, encoding="utf-8")
    return skill_file


def test_discovery_precedence_lockfile_and_locations(tmp_path: Path):
    _write_skill(
        tmp_path / ".agent" / "skills",
        "alpha-skill",
        "---\nname: alpha-skill\ndescription: Local alpha\n---\nAlpha body",
    )
    _write_skill(
        tmp_path / ".agents" / "skills",
        "alpha-skill",
        "---\nname: alpha-skill\ndescription: Agents alpha\n---\nShadowed body",
    )
    _write_skill(
        tmp_path / ".agents" / "skills",
        "beta-skill",
        "---\nname: beta-skill\ndescription: Skillforge beta\n---\nBeta body",
    )
    (tmp_path / ".agents" / "skills" / ".skillforge-lock.json").write_text(
        json.dumps({"skills": {"beta-skill": {"version": "1.0.0"}}}),
        encoding="utf-8",
    )
    _write_skill(
        tmp_path / ".claude" / "skills",
        "gamma-skill",
        "---\nname: gamma-skill\ndescription: Workspace gamma\n---\nGamma body",
    )

    skills = discover_skills(tmp_path)
    by_name = {s.name: s for s in skills}

    assert set(by_name.keys()) == {"alpha-skill", "beta-skill", "gamma-skill"}
    assert by_name["alpha-skill"].source == "dagger_local"
    assert by_name["alpha-skill"].description == "Local alpha"
    assert by_name["alpha-skill"].locations == [
        ".agent/skills/alpha-skill/SKILL.md",
        ".agents/skills/alpha-skill/SKILL.md",
    ]
    assert by_name["beta-skill"].source == "skillforge"
    assert by_name["gamma-skill"].source == "workspace"


def test_frontmatter_validation_errors(tmp_path: Path):
    _write_skill(
        tmp_path / ".agent" / "skills",
        "bad-yaml",
        "---\nname: [unclosed\n---\nBody",
    )
    _write_skill(
        tmp_path / ".agent" / "skills",
        "no-desc",
        "---\nname: no-desc\n---\nBody",
    )
    _write_skill(
        tmp_path / ".agent" / "skills",
        "folder-a",
        "---\nname: folder-b\ndescription: Mismatch\n---\nBody",
    )

    skills = {s.path: s for s in discover_skills(tmp_path)}
    assert skills[".agent/skills/bad-yaml/SKILL.md"].valid is False
    assert skills[".agent/skills/bad-yaml/SKILL.md"].validation_errors

    no_desc = skills[".agent/skills/no-desc/SKILL.md"]
    assert no_desc.valid is False
    assert any("description" in err for err in no_desc.validation_errors)

    mismatch = skills[".agent/skills/folder-a/SKILL.md"]
    assert mismatch.valid is False
    assert any("does not match folder" in err for err in mismatch.validation_errors)


def test_match_skills_and_read_only_guardrail(tmp_path: Path):
    _write_skill(
        tmp_path / ".agent" / "skills",
        "safe-audit",
        (
            "---\nname: safe-audit\ndescription: Read-only audit checklist\n"
            "allowed-tools: read search\n---\nInspect files only."
        ),
    )
    _write_skill(
        tmp_path / ".agent" / "skills",
        "mutating-fix",
        (
            "---\nname: mutating-fix\ndescription: Edit and shell fixes\n"
            "allowed-tools: read write shell\n---\nRun shell and edit files."
        ),
    )
    _write_skill(
        tmp_path / ".agent" / "skills",
        "scoped-mutator",
        (
            "---\nname: scoped-mutator\ndescription: Scoped bash and MCP rename\n"
            "allowed-tools: Bash(git:*) mcp__codescalpel__rename_symbol\n---\nMutate."
        ),
    )

    skills = discover_skills(tmp_path)
    rw_matched = match_skills(skills, required_skills=["mutating-fix", "safe-audit"], read_only=False)
    assert [s.name for s in rw_matched] == ["mutating-fix", "safe-audit"]

    ro_matched = match_skills(
        skills,
        required_skills=["mutating-fix", "scoped-mutator", "safe-audit"],
        read_only=True,
    )
    assert [s.name for s in ro_matched] == ["safe-audit"]

    prompt_block = format_matched_skills_for_prompt(ro_matched, read_only=True, max_chars=500)
    assert "[READ-ONLY GUARDRAIL]" in prompt_block
    assert "### Skill: safe-audit" in prompt_block
    assert len(prompt_block) <= 500

    planner_catalog = format_skills_for_planner(skills, max_chars=2400)
    assert "- safe-audit: Read-only audit checklist" in planner_catalog


def test_api_skills_endpoint(tmp_path: Path, monkeypatch):
    _write_skill(
        tmp_path / ".agent" / "skills",
        "dagger-local-one",
        "---\nname: dagger-local-one\ndescription: Local skill\n---\nBody",
    )
    _write_skill(
        tmp_path / ".agents" / "skills",
        "forged-one",
        "---\nname: forged-one\ndescription: Forged skill\n---\nBody",
    )
    (tmp_path / ".agents" / "skills" / ".skillforge-lock.json").write_text(
        json.dumps({"skills": ["forged-one"]}),
        encoding="utf-8",
    )

    monkeypatch.chdir(tmp_path)
    app = create_app(db_path=tmp_path / "dagger.sqlite3", checkpoint_path=tmp_path / "cp.sqlite3")
    with TestClient(app) as client:
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 2
        assert payload["sources"] == {"dagger_local": 1, "skillforge": 1}
        names = {item["name"] for item in payload["skills"]}
        assert names == {"dagger-local-one", "forged-one"}


def test_api_skills_real_repo_workspace(tmp_path: Path):
    app = create_app(db_path=tmp_path / "dagger.sqlite3", checkpoint_path=tmp_path / "cp.sqlite3")
    with TestClient(app) as client:
        resp = client.get("/api/skills")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["count"] == 14
        assert payload["sources"] == {"dagger_local": 5, "skillforge": 9}
        for skill in payload["skills"]:
            assert skill["valid"] is True
            if skill["source"] == "skillforge":
                assert len(skill["locations"]) == 2


@pytest.mark.asyncio
async def test_orchestrator_and_pm_skill_injection(tmp_path: Path):
    _write_skill(
        tmp_path / ".agent" / "skills",
        "audit-helper",
        (
            "---\nname: audit-helper\ndescription: Read-only verification helper\n"
            "allowed-tools: read search\n---\nCheck invariants carefully."
        ),
    )
    _write_skill(
        tmp_path / ".agent" / "skills",
        "shell-helper",
        (
            "---\nname: shell-helper\ndescription: Shell runner\n"
            "allowed-tools: shell\n---\nRun bash commands."
        ),
    )

    captured_prompts: list[str] = []

    async def fake_planner(prompt: str) -> str:
        captured_prompts.append(prompt)
        return json.dumps({
            "milestones": [
                {
                    "milestone_id": "m1",
                    "title": "Audit",
                    "description": "Audit code",
                    "required_skills": ["audit-helper", "shell-helper"],
                    "depends_on": [],
                    "dod": {"acceptance_criteria": ["ok"], "expected_artifacts": [], "verification_commands": ["true"]},
                },
                {
                    "milestone_id": "m2",
                    "title": "Wrap up",
                    "description": "Done",
                    "required_skills": [],
                    "depends_on": ["m1"],
                    "dod": {"acceptance_criteria": ["ok"], "expected_artifacts": [], "verification_commands": ["true"]},
                },
            ]
        })

    pm = ProjectManager(workspace=tmp_path, planner=fake_planner)
    db = DatabaseManager(tmp_path / "dagger.sqlite3")
    await db.initialize()
    ro_container = AgentContainer(
        container_id="ro-auditor",
        name="ro-auditor",
        role="auditor",
        provider="claude",
        allowed_tools=["read", "search"],
        strengths=["audit-helper", "shell-helper"],
    )
    await db.upsert_container(ro_container)

    task = KanbanTask(task_id="t1", title="Test", description="Test skills")
    await db.upsert_task(task)

    orch = Orchestrator(
        db=db,
        registry=SimpleNamespace(),
        project_manager=pm,
        worktree_path=tmp_path,
        bus_path=tmp_path / "bus",
    )

    plan_state = await orch._plan({"kanban_id": "t1", "run_id": "r1"})
    assert len(plan_state["milestones"]) == 2
    assert "Available Workspace Skills:" in captured_prompts[0]
    assert "audit-helper" in captured_prompts[0]

    captured_envelopes = []

    async def fake_run_provider(container, envelope):
        captured_envelopes.append(envelope)
        return "ok"

    orch._run_provider = fake_run_provider  # type: ignore[method-assign]
    milestone = MilestoneTask.model_validate(plan_state["milestones"][0])
    await orch._work({
        "kanban_id": "t1",
        "run_id": "r1",
        "task": {**milestone.model_dump(mode="json"), "_hop_count": 0},
    })

    assert len(captured_envelopes) == 1
    prompt_data = json.loads(captured_envelopes[0].prompt)
    assert prompt_data["matched_skills"] == ["audit-helper"]
    assert "[READ-ONLY GUARDRAIL]" in prompt_data["skill_guidance"]
    assert "shell-helper" not in prompt_data["matched_skills"]
    await db.close()


@pytest.mark.parametrize(
    ("context_text", "read_only", "expected_skills"),
    [
        ("Audit bus.py", True, {"dagger-read-only-audit", "using-code-scalpel"}),
        ("Audit bus.py", False, {"dagger-read-only-audit", "using-code-scalpel"}),
        (
            "Decompose work across parallel swarm workers",
            False,
            {"dagger-swarm-coordination"},
        ),
    ],
)
def test_real_repo_skill_trigger_eval(
    context_text: str,
    read_only: bool,
    expected_skills: set[str],
):
    from dagger.skills import SkillRegistry

    repo_root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry(repo_root)
    matched = registry.match(
        required_skills=[],
        context_text=context_text,
        read_only=read_only,
    )
    matched_names = {s.name for s in matched}
    assert expected_skills.issubset(matched_names)


def test_read_only_mcp_tool_scoping_and_guardrail():
    from dagger.skills import (
        MUTATING_CODESCALPEL_MCP_TOOLS,
        READ_ONLY_CODESCALPEL_MCP_TOOLS,
        SkillRegistry,
        format_matched_skills_for_prompt,
    )

    repo_root = Path(__file__).resolve().parents[1]
    registry = SkillRegistry(repo_root)
    matched = registry.match(
        required_skills=["dagger-read-only-audit", "using-code-scalpel"],
        context_text="Audit bus.py",
        read_only=True,
    )
    allowed = registry.resolve_allowed_mcp_tools(matched, read_only=True)
    assert allowed == list(READ_ONLY_CODESCALPEL_MCP_TOOLS)
    for mutating in MUTATING_CODESCALPEL_MCP_TOOLS:
        assert mutating not in allowed

    prompt_text = format_matched_skills_for_prompt(
        matched, read_only=True, allowed_mcp_tools=allowed
    )
    assert "[READ-ONLY GUARDRAIL]" in prompt_text
    for ro_tool in READ_ONLY_CODESCALPEL_MCP_TOOLS:
        assert ro_tool in prompt_text
    for mutating_name in ("rename_symbol", "update_symbol", "delete_symbol", "simulate_refactor"):
        assert mutating_name in prompt_text

    rw_allowed = registry.resolve_allowed_mcp_tools(matched, read_only=False)
    for ro_tool in READ_ONLY_CODESCALPEL_MCP_TOOLS:
        assert ro_tool in rw_allowed
    for mutating in MUTATING_CODESCALPEL_MCP_TOOLS:
        assert mutating in rw_allowed

    # Empty skills -> empty tools
    assert registry.resolve_allowed_mcp_tools([], read_only=True) == []
    assert registry.resolve_allowed_mcp_tools([], read_only=False) == []

    # Wildcard / mutating / mixed-case declared MCP tools under read_only=True
    from dagger.skills import LoadedSkill
    tricky_skill = LoadedSkill(
        name="tricky-audit",
        description="Tricky audit",
        source="workspace",
        path="tricky/SKILL.md",
        allowed_tools=[
            "mcp__codescalpel__*",
            "mcp__codescalpel__write_file",
            "MCP__CODESCALPEL__EXTRACT_CODE",
            "mcp__codescalpel__rename_symbol",
        ],
    )
    tricky_allowed = registry.resolve_allowed_mcp_tools([tricky_skill], read_only=True)
    assert tricky_allowed == list(READ_ONLY_CODESCALPEL_MCP_TOOLS)
    prompt_filtered = format_matched_skills_for_prompt(
        [tricky_skill],
        read_only=True,
        allowed_mcp_tools=["mcp__codescalpel__rename_symbol", "MCP__CODESCALPEL__EXTRACT_CODE"],
    )
    assert "mcp__codescalpel__rename_symbol" not in prompt_filtered
    assert "mcp__codescalpel__extract_code" in prompt_filtered


@pytest.mark.asyncio
async def test_orchestrator_emits_skill_telemetry_and_bus_preserves_it(tmp_path: Path):
    from dagger.bus import EventBus
    from dagger.skills import MUTATING_CODESCALPEL_MCP_TOOLS, READ_ONLY_CODESCALPEL_MCP_TOOLS

    _write_skill(
        tmp_path / ".agent" / "skills",
        "dagger-read-only-audit",
        "---\nname: dagger-read-only-audit\ndescription: Read-only audit\nallowed-tools: read search\n---\nAudit only.",
    )
    _write_skill(
        tmp_path / ".agents" / "skills",
        "using-code-scalpel",
        "---\nname: using-code-scalpel\ndescription: Code Scalpel MCP\n---\nUse Code Scalpel.",
    )

    db = DatabaseManager(tmp_path / "dagger.sqlite3")
    await db.initialize()
    try:
        for existing in await db.list_containers():
            if existing.container_id != "ro-auditor":
                await db.upsert_container(existing.model_copy(update={"active": False}))
        ro_container = AgentContainer(
            container_id="ro-auditor",
            name="ro-auditor",
            role="auditor",
            provider="claude",
            model="claude-opus",
            allowed_tools=["read", "search"],
            strengths=["dagger-read-only-audit", "using-code-scalpel"],
        )
        await db.upsert_container(ro_container)
        await db.upsert_task(KanbanTask(task_id="t1", title="Audit", description="Audit bus.py"))

        bus = EventBus()
        emitted_events: list[dict] = []
        orig_publish = bus.publish

        async def capture_publish(event: dict) -> None:
            emitted_events.append(event)
            await orig_publish(event)

        bus.publish = capture_publish  # type: ignore[method-assign]

        orch = Orchestrator(
            db=db,
            registry=SimpleNamespace(),
            project_manager=ProjectManager(workspace=tmp_path),
            worktree_path=tmp_path,
            bus_path=tmp_path / "bus",
            bus=bus,
        )
        captured_envelopes = []

        async def fake_run_provider(container, envelope):
            captured_envelopes.append(envelope)
            return "audit ok"

        orch._run_provider = fake_run_provider  # type: ignore[method-assign]
        milestone = MilestoneTask(
            milestone_id="m-audit",
            title="Audit bus.py",
            description="Audit bus.py for invariants",
            required_skills=["dagger-read-only-audit", "using-code-scalpel"],
            dod={"acceptance_criteria": ["ok"], "expected_artifacts": [], "verification_commands": ["true"]},
        )
        await orch._work({
            "kanban_id": "t1",
            "run_id": "r1",
            "task": {**milestone.model_dump(mode="json"), "_hop_count": 0},
        })

        assert len(captured_envelopes) == 1
        assert captured_envelopes[0].container_id == "ro-auditor"
        assert captured_envelopes[0].allowed_mcp_tools == list(READ_ONLY_CODESCALPEL_MCP_TOOLS)
        for mutating in MUTATING_CODESCALPEL_MCP_TOOLS:
            assert mutating not in captured_envelopes[0].allowed_mcp_tools

        worker_spawned = [
            e for e in emitted_events
            if e["event_type"] == "NODE_SPAWNED"
            and e["target_node"] == "agent:ro-auditor"
            and e["metadata"].get("milestone_id") == "m-audit"
        ]
        worker_edges = [
            e for e in emitted_events
            if e["event_type"] == "EDGE_TRAVERSED"
            and e["target_node"] == "agent:ro-auditor"
            and e["metadata"].get("milestone_id") == "m-audit"
        ]
        assert worker_spawned
        assert worker_edges
        for ev in (*worker_spawned, *worker_edges):
            assert ev["metadata"]["skills"] == ["dagger-read-only-audit", "using-code-scalpel"]
            assert ev["metadata"]["allowed_mcp_tools"] == list(READ_ONLY_CODESCALPEL_MCP_TOOLS)

        # Verify both agent:ro-auditor and worker:m-audit nodes have telemetry,
        # and bus.update_node preserves non-empty skills/allowed_mcp_tools across empty updates
        await bus.update_node({
            "id": "r1:worker:m-audit",
            "data": {"label": "worker:m-audit", "status": "completed", "skills": [], "allowed_mcp_tools": []},
        })
        graph = await bus.get_graph()
        for node_id in ("r1:agent:ro-auditor", "r1:worker:m-audit"):
            matched_nodes = [n for n in graph["nodes"] if n["id"] == node_id]
            assert len(matched_nodes) == 1
            assert matched_nodes[0]["data"]["skills"] == ["dagger-read-only-audit", "using-code-scalpel"]
            assert matched_nodes[0]["data"]["allowed_mcp_tools"] == list(READ_ONLY_CODESCALPEL_MCP_TOOLS)

        # Verify orchestrator.get_graph(run_id="r1") replay preserves skills & allowed_mcp_tools
        replayed = await orch.get_graph(run_id="r1")
        replayed_agent = [n for n in replayed["nodes"] if n["id"] == "r1:agent:ro-auditor"]
        assert len(replayed_agent) == 1
        assert replayed_agent[0]["data"]["skills"] == ["dagger-read-only-audit", "using-code-scalpel"]
        assert replayed_agent[0]["data"]["allowed_mcp_tools"] == list(READ_ONLY_CODESCALPEL_MCP_TOOLS)
    finally:
        await db.close()
