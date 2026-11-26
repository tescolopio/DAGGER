---
name: dagger-invariant-guard
description: Verifies that changes to the DAGGER control plane preserve all six core architectural invariants. Use when reviewing or modifying src/dagger/, containers/, or agent/server configuration before running tests or merging.
metadata:
  dagger-domain: architecture
  dagger-version: "1.0.0"
---

# DAGGER Invariant Guard

Check every change against DAGGER's six architectural invariants from `AGENTS.md` before declaring work done.

## Procedure

1. **Subscription OAuth env isolation**: Verify subscription CLI credentials (e.g., Claude/Gemini OAuth tokens) are never mixed with metered API credentials (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`) in container or subprocess environments.
2. **Atomic filesystem handoffs**: Confirm message bus writes stage to `bus/tmp` and promote to `bus/outbox` via `os.replace` rather than writing directly to `bus/outbox`.
3. **Loop/hop bounds**: Check that retry loops, hop counters, and duplicate-failure detection enforce bounded limits and fail-fast escalation instead of unbounded retries.
4. **SQLite WAL mode**: Verify SQLite connections enable `PRAGMA journal_mode=WAL` and do not revert to rollback journal mode.
5. **Loopback-only server defaults**: Confirm HTTP/API servers (such as `src/dagger/server.py`) bind to `127.0.0.1` by default, never `0.0.0.0` unless explicitly configured.
6. **Least-privilege read-only auditors**: Verify auditor containers/agents use `allowed_tools: [read, search]` and rely on PM-authorized artifact materialization (`_materialize_read_only_output` in `src/dagger/agents/orchestrator.py`).

## Verification

- Run `uv run pytest -v` and `cd frontend && npm run build` (or `/verify-dagger`) after any control-plane change.
- Report any invariant violation with file path, symbol name, and the specific invariant broken.
