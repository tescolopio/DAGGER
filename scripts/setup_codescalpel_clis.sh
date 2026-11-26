#!/usr/bin/env bash
# One-time, user-level Code Scalpel registration for CLIs that cannot receive an MCP
# server per run. Claude, Copilot and Codex get it injected by DAGGER on every run.
#
#   gemini: links DAGGER's two extensions (full + read-only); the runner picks one with -e.
#   agy:    registers the server globally; read-only agy workers run in --mode plan.
#
# Idempotent. Undo with:
#   gemini extensions uninstall dagger-codescalpel dagger-codescalpel-readonly
#   agy mcp remove codescalpel
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep in sync with dagger/mcp.py CODESCALPEL_ARGS (tests/test_mcp_scoping.py enforces it).
CODESCALPEL_ARGS=(--with mcp==1.30.0 codescalpel@2.1.4 mcp)

if command -v gemini >/dev/null 2>&1; then
  for ext in dagger-codescalpel dagger-codescalpel-readonly; do
    if [ -e "${GEMINI_CLI_HOME:-$HOME}/.gemini/extensions/$ext" ]; then
      echo "gemini: $ext already linked"
    else
      gemini extensions link "$ROOT/integrations/gemini/$ext" --consent
    fi
  done
else
  echo "gemini: not installed, skipping"
fi

if command -v agy >/dev/null 2>&1; then
  agy mcp add codescalpel uvx -- "${CODESCALPEL_ARGS[@]}"
  echo "agy: codescalpel registered"
else
  echo "agy: not installed, skipping"
fi
