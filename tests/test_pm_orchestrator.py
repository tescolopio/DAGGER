import json
from dataclasses import asdict
from pathlib import Path

import pytest

from dagger.agents.project_manager import (
    Criterion,
    Plan,
    PlannedTask,
    ProjectManager,
    VerificationResult,
)
from dagger.schema import DefinitionOfDone, KanbanTask, MilestoneTask


ROOT = Path(__file__).resolve().parents[1]


def milestone(**dod):
    return MilestoneTask(
        milestone_id="one",
        title="Test feature",
        dod=DefinitionOfDone(**dod),
    )


@pytest.mark.asyncio
async def test_decompose_produces_valid_dag_and_concrete_dod():
    async def planner(prompt):
        assert "project goal" in prompt
        assert "verification_commands" in prompt
        return json.dumps({"milestones": [
            {"milestone_id": "a", "title": "First", "description": "Set up",
             "depends_on": [], "dod": {"acceptance_criteria": ["Readme exists"],
                "expected_artifacts": ["README.md"], "verification_commands": ["test -f README.md"]}},
            {"milestone_id": "b", "title": "Second", "description": "Finish",
             "depends_on": ["a"], "dod": {"acceptance_criteria": ["Tests pass"],
                "expected_artifacts": ["README.md"], "verification_commands": ["test -s README.md"]}},
        ]})

    graph = await ProjectManager(planner=planner).decompose_goal(
        KanbanTask(title="project goal", description="Complete it")
    )
    assert [task.milestone_id for task in graph.milestones] == ["a", "b"]
    assert graph.dependencies == {"a": (), "b": ("a",)}
    assert graph.milestones[0].dod.expected_artifacts == ["README.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dependencies", [{"a": ["b"], "b": ["a"]}, {"a": ["missing"], "b": []}])
async def test_decompose_rejects_invalid_dependencies(dependencies):
    async def planner(_):
        return json.dumps({"milestones": [
            {"milestone_id": name, "title": name, "depends_on": deps,
             "dod": {"acceptance_criteria": ["exists"], "expected_artifacts": ["README.md"],
                     "verification_commands": ["test -f README.md"]}}
            for name, deps in dependencies.items()
        ]})

    with pytest.raises(ValueError):
        await ProjectManager(planner=planner).decompose_goal(KanbanTask(title="goal"))


@pytest.mark.asyncio
async def test_decompose_rejects_single_goal_shaped_milestone():
    async def planner(_):
        return json.dumps({"milestones": [
            {"milestone_id": "only", "title": "goal", "dod": {
                "acceptance_criteria": ["exists"], "expected_artifacts": ["README.md"],
                "verification_commands": ["test -f README.md"]}}
        ]})

    with pytest.raises(ValueError, match="at least two"):
        await ProjectManager(planner=planner).decompose_goal(KanbanTask(title="goal"))


@pytest.mark.asyncio
async def test_audit_collects_actual_evidence_and_passes():
    manager = ProjectManager(workspace=ROOT)
    task = milestone(acceptance_criteria=["README names DAGGER"], expected_artifacts=["README.md"],
                     verification_commands=["grep -F -q -- DAGGER README.md"])
    audit = await manager.audit_milestone(task, ROOT)
    assert audit.status == "DOD_PASSED"
    assert audit.command_results[0].returncode == 0
    assert audit.criteria_results[0].passed and audit.artifact_results[0].passed
    assert not audit.human_review_required


@pytest.mark.asyncio
async def test_audit_failures_remediate_then_gate_at_hop_limit():
    manager = ProjectManager(workspace=ROOT)
    task = milestone(acceptance_criteria=["tests succeed"], expected_artifacts=["missing-artifact"],
                     verification_commands=["test -f missing-artifact"])
    report = await manager.audit_milestone(task, ROOT, hop_count=1, max_hops=2)
    assert report.status == "DOD_FAILED"
    assert "missing-artifact" in report.remediation_brief and "failed" in report.remediation_brief
    assert not report.human_review_required
    report = await manager.audit_milestone(task, ROOT, hop_count=2, max_hops=2)
    assert report.human_review_required


@pytest.mark.asyncio
async def test_unverifiable_criterion_requires_immediate_human_review():
    manager = ProjectManager(workspace=ROOT)
    task = milestone(acceptance_criteria=["Unverifiable"], expected_artifacts=["README.md"],
                     verification_commands=[])
    report = await manager.audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert report.human_review_required
    assert not report.criteria_results[0].passed


@pytest.mark.asyncio
async def test_empty_artifacts_and_missing_executable_fail_closed():
    manager = ProjectManager(workspace=ROOT)
    task = milestone(acceptance_criteria=["Something is checked"], expected_artifacts=[],
                     verification_commands=["dagger-nonexistent-command"])
    report = await manager.audit_milestone(task, ROOT, max_hops=0)
    assert report.status == "DOD_FAILED"
    assert report.human_review_required
    assert report.command_results[0].returncode != 0


@pytest.mark.asyncio
async def test_shell_operators_are_rejected_not_silently_ignored():
    manager = ProjectManager(workspace=ROOT)
    task = milestone(acceptance_criteria=["both commands succeed"], expected_artifacts=["README.md"],
                     verification_commands=["test -f README.md && true"])
    report = await manager.audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert report.command_results[0].returncode is None
    assert report.human_review_required


@pytest.mark.asyncio
@pytest.mark.parametrize("check", [
    "python -c 'print(1)'",
    "test -f README.md; echo accepted",
    "test -f /etc/passwd",
    "grep --include=README.md DAGGER README.md",
    "test -f README*",
])
async def test_unapproved_or_unsafe_checks_fail_closed(check):
    task = milestone(acceptance_criteria=["checked"], expected_artifacts=["README.md"],
                     verification_commands=[check])
    report = await ProjectManager(workspace=ROOT).audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert report.human_review_required
    assert report.command_results[0].returncode is None


@pytest.mark.asyncio
async def test_low_level_verifier_also_rejects_shell_executables():
    criterion = Criterion("do not run", ("/bin/sh", "-c", "true"))
    task = PlannedTask("id", "work", (criterion,))
    result = (await ProjectManager(workspace=ROOT).verify(task))[0]
    assert not result.passed
    assert result.returncode is None
    assert "approved set" in result.stderr


@pytest.mark.asyncio
async def test_verifier_uses_minimal_environment(monkeypatch):
    import asyncio

    original = asyncio.create_subprocess_exec
    captured = []

    async def record(*args, **kwargs):
        captured.append((args, kwargs["env"]))
        return await original(*args, **kwargs)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "do-not-inherit")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", record)
    task = milestone(acceptance_criteria=["exists"], expected_artifacts=["README.md"],
                     verification_commands=["test -f README.md"])
    report = await ProjectManager(workspace=ROOT).audit_milestone(task, ROOT)
    assert report.status == "DOD_PASSED"
    assert captured[0][0][0] == "/usr/bin/test"
    assert set(captured[0][1]) == {"PATH", "LANG"}


@pytest.mark.asyncio
async def test_rejects_artifacts_and_commands_outside_worktree():
    manager = ProjectManager(workspace=ROOT, command_timeout=0.1)
    task = milestone(acceptance_criteria=["Completes"], expected_artifacts=["../README.md"],
                     verification_commands=["test -f ../README.md"])
    report = await manager.audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert not report.artifact_results[0].passed
    assert "inside the worktree" in report.command_results[0].stderr


@pytest.mark.asyncio
async def test_existing_plan_can_roundtrip_and_audit():
    criterion = Criterion("runs", ("test", "-f", "README.md"))
    plan = Plan("goal", (PlannedTask("one", "work", (criterion,)),))
    restored = Plan.from_dict(asdict(plan))
    report = await ProjectManager(workspace=ROOT).audit(restored)
    assert restored == plan
    assert report.passed and report.results[0].returncode == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("max_hops,expected", [(1, "hop_limit"), (2, "passed")])
async def test_standalone_remediates_with_evidence_and_respects_hop_limit(max_hops, expected):
    instructions = []

    async def planner(_):
        return json.dumps({"tasks": [{"id": "task", "description": "work",
                           "definition_of_done": [{"description": "checked", "command": ["test", "-f", "README.md"]}]}]})

    async def worker(prompt):
        instructions.append(prompt)
        return "worked"

    manager = ProjectManager(planner=planner, worker=worker, workspace=ROOT, max_hops=max_hops)

    async def verify(task):
        passed = len(instructions) > 1
        return (VerificationResult(task.id, task.definition_of_done[0], passed,
                                   0 if passed else 1, "", "" if passed else "failed"),)

    manager.verify = verify
    report = await manager.run("goal")
    assert report.status == expected
    assert report.hops == max_hops
    if max_hops > 1:
        assert "failed" in instructions[1]


@pytest.mark.asyncio
async def test_real_pm_plan_and_audit_integrate_with_graph():
    from dagger.graph import build_graph

    async def planner(_):
        return json.dumps({"milestones": [
            {"milestone_id": ident, "title": ident, "depends_on": dependencies,
             "dod": {"acceptance_criteria": ["README names DAGGER"],
                     "expected_artifacts": ["README.md"],
                     "verification_commands": ["grep -F -q -- DAGGER README.md"]}}
            for ident, dependencies in (("first", []), ("second", ["first"]))
        ]})

    pm = ProjectManager(planner=planner, workspace=ROOT)
    seen = []

    async def graph_plan(state):
        dag = await pm.decompose_goal(KanbanTask(title=state["goal"]))
        return {"milestones": [task.model_dump(mode="json") for task in dag.milestones],
                "dependencies": dag.dependencies}

    async def graph_worker(state):
        seen.append(state["task"]["milestone_id"])
        return "finished"

    async def graph_audit(task, hop):
        model = MilestoneTask.model_validate({k: v for k, v in task.items() if not k.startswith("_")})
        return asdict(await pm.audit_milestone(model, ROOT, hop + 1, 2))

    graph = build_graph(graph_plan, graph_worker, graph_audit, max_hops=2)
    config = {"configurable": {"thread_id": "pm-integration"}}
    await graph.ainvoke({"run_id": "r", "kanban_id": "k", "goal": "Plan work"}, config)
    state = await graph.aget_state(config)
    assert seen == ["first", "second"]
    assert all(report["status"] == "DOD_PASSED" for report in state.values["audits"].values())
    assert state.next == ("human_review_gate",)
