---
name: dagger-swarm-coordination
description: Plans and coordinates DAGGER multi-agent swarms with disjoint file ownership, tiered model routing, concurrency caps, and peer-to-peer QA handoffs. Use when decomposing work across swarm workers or running /verify-dagger gates.
metadata:
  dagger-domain: orchestration
  dagger-version: "1.0.0"
---

# DAGGER Swarm Coordination

Coordinate DAGGER swarms according to `AGENTS.md`'s Tiered Model-Routing Policy and concurrency rules.

## Planning & Routing

1. **Disjoint file ownership**: Assign each worker non-overlapping files or directories; never let two workers edit the same file concurrently.
2. **Tiered model routing**:
   - Pin lightweight models (e.g., Gemini 4 Hydrogen / Flash at low effort) for parallel implementation workers.
   - Reserve high-effort models (Gemini 4 Neon / Claude Opus at high effort) for coordination and QA verification.
3. **Concurrency cap**: Keep active concurrent swarm workers capped at 2–3.
4. **Peer-to-peer handoffs**: Route author -> `@qa-verifier` directly on the board; tag `@coordinator` only for final deliverables or blockers.

## Verification Gate

- Before declaring a swarm task complete, run `/verify-dagger` (`.agent/workflows/verify-dagger.md`), which executes `uv run pytest -v` and `cd frontend && npm run build`.
