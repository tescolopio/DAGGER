"""Planning and evidence-based verification for project work.

Milestone verification commands run as shell checks on the local host. Only
trusted plans should be audited: the worktree is not a sandbox and shell
commands have the permissions of the DAGGER process.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence


Executor = Callable[[str], Awaitable[str]]

SECRETS_ENV_VARS = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "GITHUB_TOKEN", "GH_TOKEN", "TAVILY_API_KEY",
})


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _items(value: object, field: str) -> Sequence:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"{field} must be a non-empty array")
    return value


@dataclass(frozen=True)
class Criterion:
    description: str
    command: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "description", _text(self.description, "criterion description"))
        command = _items(self.command, "criterion command")
        object.__setattr__(self, "command", tuple(_text(arg, "command argument") for arg in command))

    @classmethod
    def from_dict(cls, value: Mapping) -> Criterion:
        if not isinstance(value, Mapping):
            raise ValueError("criterion must be an object")
        return cls(description=value.get("description"), command=value.get("command"))


@dataclass(frozen=True)
class PlannedTask:
    id: str
    description: str
    definition_of_done: tuple[Criterion, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _text(self.id, "task id"))
        object.__setattr__(self, "description", _text(self.description, "task description"))
        criteria = _items(self.definition_of_done, "definition_of_done")
        if not all(isinstance(item, Criterion) for item in criteria):
            raise ValueError("definition_of_done must contain Criterion objects")
        object.__setattr__(self, "definition_of_done", tuple(criteria))

    @classmethod
    def from_dict(cls, value: Mapping) -> PlannedTask:
        if not isinstance(value, Mapping):
            raise ValueError("task must be an object")
        return cls(
            id=value.get("id"),
            description=value.get("description"),
            definition_of_done=tuple(
                Criterion.from_dict(item)
                for item in _items(value.get("definition_of_done"), "definition_of_done")
            ),
        )


@dataclass(frozen=True)
class Plan:
    goal: str
    tasks: tuple[PlannedTask, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text(self.goal, "goal"))
        tasks = _items(self.tasks, "tasks")
        if not all(isinstance(task, PlannedTask) for task in tasks):
            raise ValueError("tasks must contain PlannedTask objects")
        if len({task.id for task in tasks}) != len(tasks):
            raise ValueError("task ids must be unique")
        object.__setattr__(self, "tasks", tuple(tasks))

    @classmethod
    def from_dict(cls, value: Mapping, *, goal: str | None = None) -> Plan:
        if not isinstance(value, Mapping):
            raise ValueError("plan must be an object")
        return cls(
            goal=goal if goal is not None else value.get("goal"),
            tasks=tuple(PlannedTask.from_dict(task) for task in _items(value.get("tasks"), "tasks")),
        )


@dataclass(frozen=True)
class VerificationResult:
    task_id: str
    criterion: Criterion
    passed: bool
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class Remediation:
    task_id: str
    instructions: str
    evidence: tuple[VerificationResult, ...]


@dataclass(frozen=True)
class AuditReport:
    passed: bool
    results: tuple[VerificationResult, ...]
    remediation: tuple[Remediation, ...]


@dataclass(frozen=True)
class ProjectReport:
    plan: Plan
    status: str
    hops: int
    audits: tuple[AuditReport, ...]
    worker_outputs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class MilestoneDAG:
    milestones: tuple
    dependencies: dict[str, tuple[str, ...]]


@dataclass(frozen=True)
class CommandEvidence:
    command: str
    passed: bool
    returncode: int | None
    stdout: str
    stderr: str


@dataclass(frozen=True)
class CheckEvidence:
    item: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class MilestoneAudit:
    status: str
    remediation_brief: str
    human_review_required: bool
    criteria_results: tuple[CheckEvidence, ...]
    artifact_results: tuple[CheckEvidence, ...]
    command_results: tuple[CommandEvidence, ...]


class ProjectManager:
    """Create structured plans and verify their explicit definition of done.

    Executors are async callables accepting a prompt and returning text. The
    optional standalone ``run`` counts one hop per worker dispatch.
    """

    def __init__(
        self,
        planner: Executor | None = None,
        worker: Executor | None = None,
        workspace: str | Path = ".",
        max_hops: int = 3,
        command_timeout: float = 60,
    ) -> None:
        if not isinstance(max_hops, int) or isinstance(max_hops, bool) or max_hops < 0:
            raise ValueError("max_hops must be a non-negative integer")
        if command_timeout <= 0:
            raise ValueError("command_timeout must be positive")
        self.planner = planner
        self.worker = worker
        self.workspace = Path(workspace).resolve()
        if not self.workspace.is_dir():
            raise ValueError("workspace must be an existing directory")
        self.max_hops = max_hops
        self.command_timeout = command_timeout

    async def decompose_goal(self, task, executor: Executor | None = None) -> MilestoneDAG:
        """Ask the planner for a validated DAG of shared MilestoneTask models."""
        from dagger.schema import MilestoneTask

        planner = executor or self.planner
        if planner is None:
            raise ValueError("a planner executor is required")
        prompt = (
            "Decompose this project into at least two actionable milestone DAG nodes. Return ONLY JSON "
            'as {"milestones":[{"milestone_id":"unique-id","title":"...",'
            '"description":"...","required_skills":["..."],"depends_on":["id"],'
            '"dod":{"acceptance_criteria":["..."],"expected_artifacts":["relative/path"],'
            '"verification_commands":["executable arguments"]}}]}. '
            "Dependencies must reference other milestones and be acyclic. Give each "
            "acceptance criterion a corresponding executable verification command at "
            "the same array index; commands must exit zero only if the criterion is met. "
            "Commands execute as shell checks in the worktree; artifact paths must "
            "be worktree-relative. "
            "Include concrete, independently checkable criteria and commands.\n"
            + json.dumps({"title": task.title, "description": task.description}, ensure_ascii=False)
        )
        response = await planner(prompt)
        try:
            value = json.loads(response)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("planner returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise ValueError("milestone plan must be an object")
        raw_milestones = _items(value.get("milestones"), "milestones")
        if len(raw_milestones) < 2:
            raise ValueError("project decomposition must contain at least two milestones")
        milestones, dependencies = [], {}
        for raw in raw_milestones:
            if not isinstance(raw, dict):
                raise ValueError("milestone must be an object")
            data = dict(raw)
            milestone_id = _text(data.get("milestone_id"), "milestone_id")
            if milestone_id in dependencies:
                raise ValueError("milestone ids must be unique")
            deps = data.pop("depends_on", [])
            if not isinstance(deps, list) or not all(isinstance(dep, str) and dep.strip() for dep in deps):
                raise ValueError("depends_on must be an array of milestone ids")
            dependencies[milestone_id] = tuple(dep.strip() for dep in deps)
            milestone = MilestoneTask.model_validate(data)
            dod = milestone.dod
            if (not dod.acceptance_criteria or not dod.expected_artifacts
                    or len(dod.verification_commands) < len(dod.acceptance_criteria)):
                raise ValueError("milestones require artifacts and a verification command for every criterion")
            for item in [*dod.acceptance_criteria, *dod.verification_commands, *dod.expected_artifacts]:
                _text(item, "definition of done entry")
            milestones.append(milestone)
        visiting, visited = set(), set()

        def visit(node: str) -> None:
            if node in visiting:
                raise ValueError("milestone dependencies must be acyclic")
            if node in visited:
                return
            if node not in dependencies:
                raise ValueError(f"unknown milestone dependency: {node}")
            visiting.add(node)
            for dep in dependencies[node]:
                visit(dep)
            visiting.remove(node)
            visited.add(node)

        for node in dependencies:
            visit(node)
        return MilestoneDAG(tuple(milestones), dependencies)

    async def audit_milestone(
        self, milestone, worktree_path: str | Path, hop_count: int = 0, max_hops: int | None = None
    ) -> MilestoneAudit:
        """Audit a milestone using command exits and workspace-relative artifacts.

        Criterion i is verified by command i. Missing evidence always fails;
        model-generated claims and worker output are never treated as proof.
        """
        limit = self.max_hops if max_hops is None else max_hops
        if (not isinstance(hop_count, int) or isinstance(hop_count, bool) or hop_count < 0
                or not isinstance(limit, int) or isinstance(limit, bool) or limit < 0):
            raise ValueError("hop_count and max_hops must be non-negative integers")
        worktree = Path(worktree_path).resolve()
        if not worktree.is_dir():
            raise ValueError("worktree_path must be an existing directory")
        checker = ProjectManager(workspace=worktree, command_timeout=self.command_timeout)
        commands = []
        for command in milestone.dod.verification_commands:
            try:
                criterion = Criterion(command, ("/bin/sh", "-c", _text(command, "verification command")))
            except ValueError as exc:
                commands.append(CommandEvidence(command, False, None, "", str(exc)))
                continue
            task = PlannedTask(milestone.milestone_id, milestone.title, (criterion,))
            result = (await checker.verify(task))[0]
            commands.append(CommandEvidence(command, result.passed, result.returncode, result.stdout, result.stderr))
        criteria = []
        for index, item in enumerate(milestone.dod.acceptance_criteria):
            passed = bool(item.strip()) and index < len(commands) and commands[index].passed
            detail = f"verified by command {index + 1}" if passed else f"command {index + 1} missing or failed"
            criteria.append(CheckEvidence(item, passed, detail))
        if not criteria:
            criteria.append(CheckEvidence("(missing acceptance criteria)", False, "No acceptance criteria supplied"))
        artifacts = []
        for item in milestone.dod.expected_artifacts:
            try:
                path = Path(item)
                target = (worktree / path).resolve()
                inside = bool(item.strip()) and not path.is_absolute() and target.is_relative_to(worktree)
                exists = inside and target.exists()
            except (OSError, RuntimeError, ValueError):
                exists = False
            artifacts.append(CheckEvidence(item, exists, "present" if exists else "missing or outside worktree"))
        if not artifacts:
            artifacts.append(CheckEvidence("(missing expected artifacts)", False, "No artifacts supplied"))
        unverifiable = (
            not milestone.dod.acceptance_criteria
            or any(not item.strip() for item in milestone.dod.acceptance_criteria)
            or len(commands) < len(milestone.dod.acceptance_criteria)
            or not milestone.dod.expected_artifacts
            or any(result.returncode in (None, 126, 127) for result in commands[:len(criteria)])
        )
        passed = not unverifiable and all(result.passed for result in (*criteria, *artifacts, *commands))
        failures = [
            f"{result.item}: {result.detail}"
            for result in (*criteria, *artifacts) if not result.passed
        ]
        failures.extend(
            f"command {result.command!r} failed (exit={result.returncode}): {result.stderr or result.stdout}"
            for result in commands if not result.passed
        )
        return MilestoneAudit(
            "DOD_PASSED" if passed else "DOD_FAILED",
            "\n".join(failures), not passed and (unverifiable or hop_count >= limit),
            tuple(criteria), tuple(artifacts), tuple(commands),
        )

    async def create_plan(self, goal: str, executor: Executor | None = None) -> Plan:
        goal = _text(goal, "goal")
        planner = executor or self.planner
        if planner is None:
            raise ValueError("a planner executor is required")
        prompt = (
            "Create an actionable plan for the goal below. Return ONLY a JSON object "
            'with a non-empty "tasks" array. Each task must have a unique string "id", '
            'a concrete "description", and a non-empty "definition_of_done" array. '
            'Each criterion must have a "description" and a "command" array of '
            "executable and arguments. Commands must independently verify the criterion "
            "in the project workspace and exit zero only on success. "
            "Do not use prose "
            "as evidence or use shell strings.\nGoal:\n" + goal
        )
        response = await planner(prompt)
        if not isinstance(response, str):
            raise ValueError("planner must return a JSON string")
        try:
            value = json.loads(response)
        except json.JSONDecodeError as exc:
            raise ValueError("planner returned invalid JSON") from exc
        return Plan.from_dict(value, goal=goal)

    async def verify(self, task: PlannedTask) -> tuple[VerificationResult, ...]:
        results = []
        for criterion in task.definition_of_done:
            env = {key: value for key, value in os.environ.items() if key not in SECRETS_ENV_VARS}
            try:
                process = await asyncio.create_subprocess_exec(
                    *criterion.command,
                    cwd=self.workspace,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=(os.name == "posix"),
                )
            except (OSError, ValueError) as exc:
                results.append(VerificationResult(task.id, criterion, False, None, "", str(exc)))
                continue
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), self.command_timeout)
                results.append(
                    VerificationResult(
                        task.id, criterion, process.returncode == 0, process.returncode,
                        stdout.decode("utf-8", errors="replace"),
                        stderr.decode("utf-8", errors="replace"),
                    )
                )
            except (TimeoutError, asyncio.CancelledError) as exc:
                if process.returncode is None:
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGKILL)
                        else:
                            process.kill()
                    except ProcessLookupError:
                        pass
                stdout, stderr = await process.communicate()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                results.append(
                    VerificationResult(
                        task.id, criterion, False, None,
                        stdout.decode("utf-8", errors="replace"),
                        stderr.decode("utf-8", errors="replace") + "\nVerification timed out",
                    )
                )
        return tuple(results)

    @staticmethod
    def instruction(task: PlannedTask, results: Sequence[VerificationResult] = ()) -> str:
        payload = {
            "task_id": task.id,
            "description": task.description,
            "definition_of_done": [
                {"description": item.description, "command": list(item.command)}
                for item in task.definition_of_done
            ],
        }
        failures = [
            {"criterion": result.criterion.description, "command": list(result.criterion.command),
             "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
            for result in results if result.task_id == task.id and not result.passed
        ]
        if failures:
            payload["verification_failures"] = failures
        return (
            "Implement the task and satisfy every definition-of-done criterion. "
            "If failures are provided, remediate them and preserve passing behavior.\n"
            + json.dumps(payload, ensure_ascii=False)
        )

    async def audit(self, plan: Plan, executor: Executor | None = None) -> AuditReport:
        # Verification, not a model's opinion, determines acceptance.
        all_results = []
        for task in plan.tasks:
            all_results.extend(await self.verify(task))
        results = tuple(all_results)
        remediation = tuple(
            Remediation(task.id, self.instruction(task, failures), failures)
            for task in plan.tasks
            if (failures := tuple(result for result in results if result.task_id == task.id and not result.passed))
        )
        return AuditReport(not remediation, results, remediation)

    async def run(self, goal: str) -> ProjectReport:
        if self.worker is None:
            raise ValueError("a worker executor is required")
        plan = await self.create_plan(goal)
        pending = list(plan.tasks)
        previous: dict[str, tuple[VerificationResult, ...]] = {}
        outputs: list[tuple[str, str]] = []
        audits: list[AuditReport] = []
        hops = 0
        while pending and hops < self.max_hops:
            task = pending.pop(0)
            hops += 1
            output = await self.worker(self.instruction(task, previous.get(task.id, ())))
            outputs.append((task.id, output))
            results = await self.verify(task)
            previous[task.id] = results
            if not all(result.passed for result in results):
                pending.append(task)
            if not pending and len(previous) == len(plan.tasks):
                # Check all tasks again: later work may have broken earlier work.
                report = await self.audit(plan)
                audits.append(report)
                if report.passed:
                    return ProjectReport(plan, "passed", hops, tuple(audits), tuple(outputs))
                for remediation in report.remediation:
                    previous[remediation.task_id] = remediation.evidence
                    pending.extend(task for task in plan.tasks if task.id == remediation.task_id)
        return ProjectReport(plan, "hop_limit", hops, tuple(audits), tuple(outputs))
