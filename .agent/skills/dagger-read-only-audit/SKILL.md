---
name: dagger-read-only-audit
description: Conducts least-privilege codebase, security, and DefinitionOfDone audits using only read and search tools, formatted for PM-authorized artifact materialization. Use when acting as a DAGGER auditor or reviewing code without write permissions.
metadata:
  dagger-domain: security
  dagger-version: "1.0.0"
---

# DAGGER Read-Only Audit

Perform codebase, security, and `DefinitionOfDone` audits under DAGGER's least-privilege auditor invariant (`allowed_tools: [read, search]`).

## Constraints

1. **Read and search only**: Never invoke shell commands, file-writing tools, or mutating MCP tools (including Code Scalpel refactoring/write tools such as `rename_symbol` or file-edit tools).
2. **PM-authorized materialization**: Format findings as structured output so the orchestrator/PM can materialize approved artifacts via `_materialize_read_only_output` (`src/dagger/agents/orchestrator.py`).
3. **Evidence-backed findings**: Cite exact file paths, line numbers, and symbol names observed during the read-only pass.

## Output Format

Report findings in three sections:
1. **Scope**: Files and symbols inspected.
2. **Findings**: Severity (`CRITICAL`, `HIGH`, `MEDIUM`, `LOW`), file/line, invariant or DoD rule affected, and recommended fix.
3. **Verdict**: `PASS` or `FAIL` with blocking items listed first.
