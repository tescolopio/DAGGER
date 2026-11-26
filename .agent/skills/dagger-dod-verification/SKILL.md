---
name: dagger-dod-verification
description: Authors safe, deterministic, worktree-relative DefinitionOfDone checks and verification commands for DAGGER MilestoneTasks. Use when defining, reviewing, or debugging MilestoneTask DefinitionOfDone criteria in src/dagger/schema.py, project_manager.py, or orchestrator.py.
metadata:
  dagger-domain: verification
  dagger-version: "1.0.0"
---

# DAGGER DoD Verification

Write `DefinitionOfDone` checks (`src/dagger/schema.py`) so `MilestoneTask` verification in `src/dagger/agents/orchestrator.py` and `src/dagger/agents/project_manager.py` is deterministic, fast, and safe.

## Rules for `DefinitionOfDone`

1. **Worktree-relative paths only**: Never hardcode absolute host paths or `/tmp` paths; all commands and file checks must resolve relative to the task's worktree root.
2. **Deterministic and idempotent**: Avoid network calls, wall-clock sleeps, or flaky external state. Prefer `uv run pytest <specific_test>`, `npm run build`, or explicit file/symbol assertions.
3. **Bounded execution**: Keep verification commands fast and scoped to the milestone's owned files so retry/hop bounds are not exhausted by timeouts.
4. **Non-mutating checks**: Verification commands must inspect or test state without leaving untracked artifacts or modifying `bus/outbox` directly.

## Example

In `src/dagger/schema.py`, `DefinitionOfDone(StrictModel)` (`extra="forbid"`, mounted as `MilestoneTask.dod`) requires `acceptance_criteria`, `expected_artifacts`, and `verification_commands` with 1-to-1 index correspondence between `acceptance_criteria[i]` and `verification_commands[i]`:

```yaml
dod:
  acceptance_criteria:
    - "Server unit tests pass"
    - "Frontend bundle builds cleanly"
  expected_artifacts:
    - "src/dagger/server.py"
  verification_commands:
    - "test -s src/dagger/server.py && uv run pytest -q tests/test_server.py"
    - "cd frontend && npm run build"
```

Avoid fragile shell arithmetic like `test "$(awk ...)" -ge N` (per `src/dagger/agents/project_manager.py`).

## Verification

- Confirm every `MilestoneTask` has at least one concrete `DefinitionOfDone` check.
- Run the exact `DefinitionOfDone` command locally inside the target worktree before marking a milestone complete.
