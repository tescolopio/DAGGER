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
    guard_verification_command,
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
async def test_empty_artifacts_are_allowed_when_criterion_has_evidence():
    async def planner(_):
        return json.dumps({"milestones": [
            {"milestone_id": ident, "title": ident, "depends_on": dependencies,
             "dod": {"acceptance_criteria": ["README exists"],
                     "verification_commands": ["test -f README.md"]}}
            for ident, dependencies in (("one", []), ("two", ["one"]))
        ]})

    manager = ProjectManager(planner=planner, workspace=ROOT)
    dag = await manager.decompose_goal(KanbanTask(title="goal"))
    report = await manager.audit_milestone(dag.milestones[0], ROOT)
    assert report.status == "DOD_PASSED"
    assert report.artifact_results == ()


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
async def test_shell_operators_are_executed_not_silently_ignored():
    manager = ProjectManager(workspace=ROOT)
    task = milestone(acceptance_criteria=["both commands succeed"], expected_artifacts=["README.md"],
                     verification_commands=["test -f README.md && false"])
    report = await manager.audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert report.command_results[0].returncode == 1
    assert not report.human_review_required


@pytest.mark.asyncio
@pytest.mark.parametrize("check", ["test -f README.md && printf verified",
                                   "printf verified | grep -q verified",
                                   "test -f README*"])
async def test_shell_checks_execute_in_worktree(check):
    task = milestone(acceptance_criteria=["checked"], expected_artifacts=["README.md"],
                     verification_commands=[check])
    report = await ProjectManager(workspace=ROOT).audit_milestone(task, ROOT)
    assert report.status == "DOD_PASSED"
    assert report.command_results[0].returncode == 0


@pytest.mark.asyncio
async def test_low_level_verifier_supports_executables():
    criterion = Criterion("runs", ("/bin/sh", "-c", "true"))
    task = PlannedTask("id", "work", (criterion,))
    result = (await ProjectManager(workspace=ROOT).verify(task))[0]
    assert result.passed
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_verifier_strips_provider_credentials_but_preserves_path(monkeypatch):
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
    assert captured[0][0] == ("/bin/sh", "-c", "test -f README.md")
    assert "ANTHROPIC_API_KEY" not in captured[0][1]
    assert captured[0][1]["PATH"]


@pytest.mark.asyncio
async def test_rejects_artifacts_outside_worktree():
    manager = ProjectManager(workspace=ROOT, command_timeout=0.1)
    task = milestone(acceptance_criteria=["Completes"], expected_artifacts=["../README.md"],
                     verification_commands=["test -f README.md"])
    report = await manager.audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert not report.artifact_results[0].passed
    assert report.command_results[0].passed


@pytest.mark.asyncio
async def test_shell_check_timeout_is_recorded():
    manager = ProjectManager(workspace=ROOT, command_timeout=0.1)
    task = milestone(acceptance_criteria=["Completes"], expected_artifacts=["README.md"],
                     verification_commands=["sleep 2"])
    report = await manager.audit_milestone(task, ROOT)
    assert report.status == "DOD_FAILED"
    assert "timed out" in report.command_results[0].stderr


@pytest.mark.asyncio
async def test_uv_pytest_check_is_passed_to_shell(monkeypatch):
    import asyncio

    calls = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"pytest passed", b""

    async def create_process(*args, **kwargs):
        calls.append((args, kwargs))
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_process)
    task = milestone(acceptance_criteria=["tests pass"], expected_artifacts=["README.md"],
                     verification_commands=["uv run pytest -v"])
    report = await ProjectManager(workspace=ROOT).audit_milestone(task, ROOT)
    assert report.status == "DOD_PASSED"
    assert calls[0][0] == ("/bin/sh", "-c", "uv run pytest -v")


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


@pytest.mark.asyncio
async def test_decompose_populates_allowed_write_paths_and_guards_shell_commands(tmp_path):
    captured_prompts = []

    async def planner(prompt):
        captured_prompts.append(prompt)
        return json.dumps({
            "milestones": [
                {
                    "milestone_id": "audit",
                    "title": "Audit codebase",
                    "depends_on": [],
                    "dod": {
                        "acceptance_criteria": [
                            "Audit report has at least 2 lines",
                            "Audit report mentions FINDINGS",
                        ],
                        "expected_artifacts": ["docs/audit.md"],
                        "verification_commands": [
                            'test "$(awk \'END{print NR}\' docs/audit.md)" -ge 2',
                            "grep -q FINDINGS docs/audit.md",
                        ],
                    },
                },
                {
                    "milestone_id": "summary",
                    "title": "Write summary",
                    "depends_on": ["audit"],
                    "dod": {
                        "acceptance_criteria": ["Summary has at least 1 line"],
                        "expected_artifacts": ["docs/summary.md"],
                        "verification_commands": [
                            "test $(awk 'END{print NR}' docs/summary.md) -ge 1",
                        ],
                    },
                },
            ]
        })

    pm = ProjectManager(planner=planner, workspace=tmp_path)
    dag = await pm.decompose_goal(KanbanTask(title="Audit repo", description="Produce reports"))

    assert "test -s <path> &&" in captured_prompts[0]
    assert 'test "$(awk ...)" -ge N' in captured_prompts[0]

    audit_m, summary_m = dag.milestones
    assert audit_m.allowed_write_paths == ["docs/audit.md"]
    assert summary_m.allowed_write_paths == ["docs/summary.md"]

    for cmd in audit_m.dod.verification_commands:
        assert cmd.startswith("test -s docs/audit.md && ")
        assert 'test "$(awk' not in cmd
        assert "test $(awk" not in cmd

    summary_cmd = summary_m.dod.verification_commands[0]
    assert summary_cmd.startswith("test -s docs/summary.md && ")
    assert "test $(awk" not in summary_cmd

    # Missing file fails cleanly with exit 1 and no `/bin/sh: test: Illegal number:`.
    missing_report = await pm.audit_milestone(audit_m, tmp_path)
    assert missing_report.status == "DOD_FAILED"
    assert all(res.returncode == 1 for res in missing_report.command_results)
    assert all("Illegal number" not in res.stderr for res in missing_report.command_results)

    # Empty file (0 bytes) is rejected by `test -s` without shell numeric errors.
    report_path = tmp_path / "docs" / "audit.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("", encoding="utf-8")
    empty_report = await pm.audit_milestone(audit_m, tmp_path)
    assert empty_report.status == "DOD_FAILED"
    assert all("Illegal number" not in res.stderr for res in empty_report.command_results)

    # Populated file satisfies both guarded checks.
    report_path.write_text("# Audit\nFINDINGS: clean\n", encoding="utf-8")
    passed_report = await pm.audit_milestone(audit_m, tmp_path)
    assert passed_report.status == "DOD_PASSED"
    assert all(res.passed for res in passed_report.command_results)


@pytest.mark.asyncio
async def test_guard_verification_command_prevents_illegal_number_on_empty_awk_output(tmp_path):
    raw_cmd = 'test "$(awk \'/MATCH/ {c++} END {if (c) print c}\' report.md)" -ge 1'
    guarded = guard_verification_command(raw_cmd, ["report.md"])
    assert guarded.startswith("test -s report.md && ")
    assert 'test "$(awk' not in guarded

    (tmp_path / "report.md").write_text("no hit here\n", encoding="utf-8")
    pm = ProjectManager(workspace=tmp_path)
    task = milestone(
        acceptance_criteria=["At least 1 match"],
        expected_artifacts=["report.md"],
        verification_commands=[raw_cmd],
    )
    failed = await pm.audit_milestone(task, tmp_path)
    assert failed.status == "DOD_FAILED"
    assert failed.command_results[0].returncode == 1
    assert "Illegal number" not in failed.command_results[0].stderr

    (tmp_path / "report.md").write_text("MATCH line\n", encoding="utf-8")
    passed = await pm.audit_milestone(task, tmp_path)
    assert passed.status == "DOD_PASSED"


@pytest.mark.asyncio
async def test_duplicate_consecutive_dod_failures_short_circuit_without_third_hop(tmp_path):
    from dagger.graph import build_graph

    async def planner(_):
        return json.dumps({
            "milestones": [
                {
                    "milestone_id": "first",
                    "title": "First",
                    "depends_on": [],
                    "dod": {
                        "acceptance_criteria": ["Output file exists"],
                        "expected_artifacts": ["output.md"],
                        "verification_commands": ["test -s output.md"],
                    },
                },
                {
                    "milestone_id": "second",
                    "title": "Second",
                    "depends_on": ["first"],
                    "dod": {
                        "acceptance_criteria": ["Second file exists"],
                        "expected_artifacts": ["second.md"],
                        "verification_commands": ["test -s second.md"],
                    },
                },
            ]
        })

    pm = ProjectManager(planner=planner, workspace=tmp_path, max_hops=3)
    worker_calls = []

    async def graph_plan(state):
        dag = await pm.decompose_goal(KanbanTask(title=state["goal"]))
        return {
            "milestones": [task.model_dump(mode="json") for task in dag.milestones],
            "dependencies": {key: list(value) for key, value in dag.dependencies.items()},
        }

    async def graph_worker(state):
        worker_calls.append((state["task"]["milestone_id"], state["task"]["_hop_count"]))
        return "unchanged"

    async def graph_audit(task, hop):
        model = MilestoneTask.model_validate({k: v for k, v in task.items() if not k.startswith("_")})
        return asdict(await pm.audit_milestone(model, tmp_path, hop + 1, 3, run_id=task.get("_run_id")))

    graph = build_graph(graph_plan, graph_worker, graph_audit, max_hops=3)
    config = {"configurable": {"thread_id": "dup-short-circuit"}}
    await graph.ainvoke({"run_id": "r-dup", "kanban_id": "k-dup", "goal": "Short circuit"}, config)
    state = await graph.aget_state(config)

    # Hop 1 and Hop 2 fail identically -> escalates immediately without burning hop 3.
    assert worker_calls == [("first", 0), ("first", 1)]
    assert state.values["attempts"]["first"] == 2
    first_audit = state.values["audits"]["first"]
    assert first_audit["status"] == "DOD_FAILED"
    assert first_audit["human_review_required"] is True
    assert "Duplicate failure signature" in first_audit["remediation_brief"]
    assert state.next == ("human_review_gate",)


@pytest.mark.asyncio
async def test_standalone_run_duplicate_failure_short_circuits_before_third_hop(tmp_path):
    worker_calls = []

    async def planner(_):
        return json.dumps({
            "tasks": [{
                "id": "task-1",
                "description": "produce artifact",
                "definition_of_done": [{"description": "artifact exists", "command": ["test", "-s", "missing.txt"]}],
            }]
        })

    async def worker(prompt):
        worker_calls.append(prompt)
        return "no progress"

    pm = ProjectManager(planner=planner, worker=worker, workspace=tmp_path, max_hops=3)
    report = await pm.run("goal")
    assert report.status == "human_review_required"
    assert report.hops == 2
    assert len(worker_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "wrapper",
    [
        lambda raw: f"```json\n{raw}\n```",
        lambda raw: f"```\n{raw}\n```",
        lambda raw: f"Here is the milestone DAG:\n\n```json\n{raw}\n```\n\nLet me know if you want changes.",
        lambda raw: f"Note: use {{worktree}} paths.\n{raw}\nDone planning.",
    ],
)
async def test_decompose_goal_parses_markdown_fenced_and_prose_wrapped_json(wrapper):
    payload = json.dumps({
        "milestones": [
            {
                "milestone_id": "m1",
                "title": "Assess state",
                "description": "Inspect code and ```sh\necho ok\n``` snippets",
                "depends_on": [],
                "dod": {
                    "acceptance_criteria": ["README exists"],
                    "expected_artifacts": ["README.md"],
                    "verification_commands": ["test -f README.md"],
                },
            },
            {
                "milestone_id": "m2",
                "title": "Plan sprints",
                "description": "Draft sprint backlog",
                "depends_on": ["m1"],
                "dod": {
                    "acceptance_criteria": ["README non-empty"],
                    "expected_artifacts": ["README.md"],
                    "verification_commands": ["test -s README.md"],
                },
            },
        ]
    })

    async def planner(_):
        return wrapper(payload)

    dag = await ProjectManager(planner=planner, workspace=ROOT).decompose_goal(
        KanbanTask(title="Assess repo", description="Build roadmap")
    )
    assert [m.milestone_id for m in dag.milestones] == ["m1", "m2"]
    assert dag.dependencies == {"m1": (), "m2": ("m1",)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_response",
    [
        "",
        "   \n  ",
        "Sorry, I could not generate a plan.",
        "```json\n{\"milestones\": [broken\n```",
        "Here is broken JSON: {not valid json}",
        None,
    ],
)
async def test_decompose_goal_rejects_invalid_or_non_json_prose(bad_response):
    async def planner(_):
        return bad_response

    with pytest.raises(ValueError, match="planner returned invalid JSON"):
        await ProjectManager(planner=planner, workspace=ROOT).decompose_goal(
            KanbanTask(title="goal")
        )


@pytest.mark.asyncio
async def test_create_plan_parses_fenced_and_wrapped_json_and_fails_closed():
    raw_plan = json.dumps({
        "tasks": [
            {
                "id": "t1",
                "description": "Verify README",
                "definition_of_done": [{"description": "README exists", "command": ["test", "-f", "README.md"]}],
            }
        ]
    })

    async def fenced(_):
        return f"Plan:\n```json\n{raw_plan}\n```"

    plan = await ProjectManager(planner=fenced, workspace=ROOT).create_plan("Ship it")
    assert [t.id for t in plan.tasks] == ["t1"]

    async def wrapped(_):
        return f"Sure! {raw_plan} Hope that helps."

    plan2 = await ProjectManager(planner=wrapped, workspace=ROOT).create_plan("Ship it")
    assert [t.id for t in plan2.tasks] == ["t1"]

    async def invalid(_):
        return "Not JSON at all"

    with pytest.raises(ValueError, match="planner returned invalid JSON"):
        await ProjectManager(planner=invalid, workspace=ROOT).create_plan("Ship it")


@pytest.mark.asyncio
async def test_pm_plan_node_emits_stderr_card_log_on_planner_exception(tmp_path):
    from dagger.agents.orchestrator import Orchestrator
    from dagger.db import DatabaseManager
    from dagger.providers import ProviderRegistry

    async def broken_planner(_):
        return "not valid json prose"

    async with DatabaseManager(tmp_path / "board.sqlite", seed_path=False) as db:
        pm = ProjectManager(planner=broken_planner, workspace=tmp_path)
        pm.provider = "agy"  # type: ignore[attr-defined]
        orchestrator = Orchestrator(
            db,
            ProviderRegistry(defaults=False),
            pm,
            worktree_path=tmp_path,
            bus_path=tmp_path / "bus",
        )
        with pytest.raises(ValueError, match="planner returned invalid JSON"):
            await orchestrator.start("run-fail-plan", "Broken goal", kanban_id="card-fail-plan")

        task = await db.get_task("card-fail-plan")
        assert task is not None
        assert task.status == "Failed"

        logs = await db.list_card_logs("card-fail-plan", run_id="run-fail-plan")
        stderr_logs = [
            entry for entry in logs
            if entry["node_id"] == "pm_plan_node" and entry["channel"] == "stderr"
        ]
        assert len(stderr_logs) == 1
        assert stderr_logs[0]["provider"] == "agy"
        assert "planner returned invalid JSON" in stderr_logs[0]["text"]
