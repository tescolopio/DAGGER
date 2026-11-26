# SkillForge Expansion Roadmap for DAGGER

Prioritized recommendations for expanding upstream `tescolopio/SkillForge` (tracked at `external/SkillForge/`) based on `external/SkillForge/docs/GAP-REPORT.md`, `external/SkillForge/docs/tool-matrix.md`, and DAGGER's adoption requirements.

## 1. P0 Platform & Tooling Gaps

| Priority | Gap | Upstream Reference | DAGGER Consumption |
| --- | --- | --- | --- |
| **P0** | **Antigravity (`agy`) target** (`--tool agy`, `.agent/skills/`, `tool-matrix.md` row) | `docs/tool-matrix.md` | Lets `install.py` install skills directly into `.agent/skills/` without manual syncing from `.agents/skills/`. |
| **P0** | **Skill safety & supply-chain scanner** | `docs/GAP-REPORT.md` Risk #2 | Prevents untrusted third-party skills or scripts from exfiltrating OAuth/API credentials or mutating `bus/outbox`. |
| **P0** | **Automated trigger & quality eval harness** | `docs/GAP-REPORT.md` Risk #5 | Measures trigger precision/recall for DAGGER's 5 local skills and upstream skills before release. |
| **P1** | **End-to-end tool discovery smoke tests** | `docs/GAP-REPORT.md` Risk #3 & #4 | Verifies Claude Code, Gemini CLI, Codex, Cursor, and `agy` actually discover installed skills and MCP configs at runtime. |

## 2. P0/P1 High-Leverage General Skills

| Priority | Proposed Skill | Scope & Purpose | DAGGER Consumption |
| --- | --- | --- | --- |
| **P0** | `mcp-security-and-scoping` | Splits read-only vs write/refactor MCP tool profiles (e.g., Code Scalpel's 23 tools). | Enforces DAGGER's least-privilege read-only auditor invariant (`allowed_tools: [read, search]`) when `codescalpel` MCP is enabled. |
| **P0** | `multi-agent-orchestration` | Disjoint file ownership, bounded retries/hops, peer-to-peer QA handoffs. | Complements `dagger-swarm-coordination` with reusable cross-repo orchestration patterns. |
| **P1** | `api-contract-and-schema-evolution` | Backward-compatible Pydantic/OpenAPI & TypeScript contract changes. | Protects `src/dagger/schema.py` and `frontend/` API boundaries. |
| **P1** | `sqlite-and-state-migrations` | Safe WAL-mode SQLite schema migrations, locking, and rollback checks. | Supports DAGGER's SQLite WAL-mode state store. |
| **P1** | `performance-and-observability` | Structured logging, latency/token budgeting, and bottleneck profiling. | Supports DAGGER container cost-tier downshifting and swarm telemetry. |
| **P1** | **GRC / Compliance & Delivery skills** | Audit trails, release checklists, and policy-as-code (`GAP-REPORT.md` missing domains). | Supports DAGGER's `DefinitionOfDone` verification and release gating. |
