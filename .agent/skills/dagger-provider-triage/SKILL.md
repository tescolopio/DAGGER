---
name: dagger-provider-triage
description: Diagnoses OAuth/CLI interactive login handoffs, quota exhaustion, rate limits, and cost-tier downshifting while preserving subscription vs metered API credential isolation. Use when a DAGGER container hits auth failures, rate limits, or needs downshift_container.
metadata:
  dagger-domain: operations
  dagger-version: "1.0.0"
---

# DAGGER Provider Triage

Diagnose and resolve provider authentication, quota exhaustion, and cost-tier downshifting without breaking DAGGER's subscription OAuth env isolation invariant.

## Triage Steps

1. **Check credential isolation first**: Verify subscription CLI OAuth tokens are not leaked into metered API env vars (`ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY`), and vice versa.
2. **Interactive OAuth handoff**: For CLI login flows, confirm `BROWSER=echo` is set so the auth URL is printed rather than spawning a headless browser, and verify the `localhost` -> `127.0.0.1` callback relay is bound to loopback only.
3. **Quota exhaustion / rate limits**: Distinguish transient 429 rate limits (bounded backoff) from hard quota exhaustion.
4. **Cost-tier downshifting**: When a high-tier model hits quota or cost limits, use `downshift_container` (`src/dagger/server.py` / `src/dagger/agents/generator.py`) to step down the container's model tier rather than killing and recreating state.

## Verification

- Confirm no credentials are printed in logs or committed to files.
- Re-test the affected container's health or auth status after triage.
