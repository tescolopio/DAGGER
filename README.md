# DAGGER

DAGGER (Dynamic Agent Graph & Gateway for Execution Routing) is a local-first
control plane for coordinating specialist agents, a SQLite Kanban board, an
atomic filesystem blackboard, and a live execution graph.

## Setup

Install Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and Node.js.
From the repository root:

```sh
uv sync
cd frontend
npm install
npm run build
cd ..
uv run dagger serve
```

The API listens on `127.0.0.1:8420` by default. To run the frontend in
development, start `npm run dev` in `frontend/` and open the URL printed by
Vite. Use `uv run dagger serve --host <address> --port <port>` only when you
intend to expose the server on another interface.

## Providers

Install and authenticate any desired official `claude`, `codex`, `gemini`, and
`copilot` CLIs locally using their subscription login flows. DAGGER launches
these binaries as subprocesses and removes their corresponding metered API key
environment variables from the child environment. An OpenAI-compatible local
server can instead be used at `http://127.0.0.1:8080/v1`; explicitly configured
API containers can also target a cloud API.

Optionally set `TAVILY_API_KEY` in the environment to enrich capability
discovery with recent external information; without it discovery uses local
probes and baseline heuristics. Run `uv run dagger refresh` to refresh the
catalog immediately, or use the refresh button in the registry.

Create a goal on the board or run `uv run dagger run "your goal"`. Milestones
are audited against their definitions of done and require human approval before
completion. Execution events can be inspected on the live graph.

### Code Scalpel MCP for workers

Every worker gets the [Code Scalpel](https://github.com/3D-Tech-Solutions/code-scalpel)
MCP server (pinned `codescalpel@2.1.4`) so it can read, trace and refactor code by
symbol in the *target* project, which needs no MCP config of its own. Read-only
containers (no `write`/`edit`/`shell` tools) get only the analysis tools; the two
file-writing tools, `update_symbol` and `rename_symbol`, are blocked by each CLI's
own permission flags, not just by prompt text. Set `DAGGER_CODESCALPEL=0` to turn
it off.

| CLI | How Code Scalpel is supplied | Read-only enforcement |
| --- | --- | --- |
| `claude` | `--mcp-config` + `--strict-mcp-config` per run | `--disallowedTools` |
| `copilot` | `--additional-mcp-config` per run | `--deny-tool` |
| `codex` | `-c mcp_servers.codescalpel.*` per run | `--sandbox read-only` |
| `gemini` | DAGGER extension, selected with `-e` | read-only extension + `--approval-mode plan` |
| `agy` | user-level `agy mcp add` | `--mode plan` |

Gemini and agy can't receive an MCP server per run, so link them once with
`./scripts/setup_codescalpel_clis.sh`. Gemini also refuses to run headless in a
folder it doesn't trust: trust the target project once by opening `gemini` there
interactively. Code Scalpel creates a `.code-scalpel/` directory in the target
project, so add it to that project's `.gitignore`.

## Security

DAGGER is intended for use on a trusted local machine. Provider agents can
modify files and execute commands. Verification commands generated in a plan
run automatically during the audit, **before** the final human approval; they
are not sandboxed. Only run trusted goals and providers, and review generated
changes before approval. The API has no built-in authentication; do not bind
it to a public or untrusted network without adding appropriate access controls.

## Development

```sh
uv run pytest -v
cd frontend && npm run build
```
