# DAGGER

DAGGER (Dynamic Agent Graph & Gateway for Execution Routing) is a local-first
control plane for coordinating specialist agents, a SQLite Kanban board, an
atomic filesystem blackboard, and a live execution graph.

## Setup

Install Python 3.11 or newer, [uv](https://docs.astral.sh/uv/), and Node.js.
From the repository root, build the UI and install the `dagger` command:

```sh
cd frontend && npm install && npm run build && cd ..
uv tool install --editable .
```

The install is editable, so `dagger` always runs this checkout: pull or edit
the code and the command picks it up (rebuild the UI after frontend changes).
Remove it with `uv tool uninstall dagger-control-plane`.

### Updating

Update DAGGER in place from any directory (no PyPI release needed):

```sh
dagger --update                              # pull latest, refresh deps & rebuild UI if changed
dagger update                                # equivalent subcommand
dagger update --skip-pull --reinstall        # rebuild/reinstall local checkout without pulling
dagger update --no-frontend                  # skip rebuilding frontend/dist
```

For editable git checkouts, `dagger update` checks for a clean branch, runs
`git pull --ff-only`, refreshes the `uv` editable install when `pyproject.toml`
or `uv.lock` changes, and rebuilds `frontend/` when UI files change (or
`dist/` is missing). For non-editable `uv` tool installs, it runs
`uv tool upgrade dagger-control-plane`. Restart any running `dagger` server
afterwards to pick up the updated version.

## Usage

Run `dagger` inside the repository you want agents to work on, the same way
you would start `claude` or `agy`:

```sh
cd ~/code/my-project
dagger                 # start DAGGER for this repo and open the UI
dagger run "goal"      # start a goal without the UI
dagger info            # show the project and where its state lives
dagger -C ../other     # work on another repo without changing directory
```

- **Project:** the git root of the current directory (or `-C <path>`).
- **State:** the board, run history, checkpoints and file bus live in
  `~/.dagger/projects/<repo>-<hash>/`, so the project tree stays clean.
  Running inside this DAGGER checkout keeps using its own `data/`.
- **Agents:** the catalog in `containers/` plus your own definitions in
  `~/.dagger/containers/` (which wins on a clash, is hot-reloaded and is
  where the generator exports). It is shared by all projects.
- **Skills:** DAGGER's SkillForge and `dagger-*` skills are always available;
  a project's own skills (`.agent/`, `.agents/`, `.claude/` `skills/`) are
  added and win on a name clash.
- **Server:** loopback only, on port 8420, or the next free port when
  another project's DAGGER holds it. `--port`, `--host` and `--no-browser`
  are available; `dagger serve` starts the server without opening a browser.

Override locations with `DAGGER_HOME` (default `~/.dagger`),
`DAGGER_DATA_DIR` (one project's state) and `DAGGER_CONTAINERS_DIR`.

## Providers

Install and authenticate any desired official `claude`, `codex`, `gemini`, and
`copilot` CLIs locally using their subscription login flows. DAGGER launches
these binaries as subprocesses and removes their corresponding metered API key
environment variables from the child environment. An OpenAI-compatible local
server can instead be used at `http://127.0.0.1:8080/v1`; explicitly configured
API containers can also target a cloud API.

Optionally set `TAVILY_API_KEY` in the environment to enrich capability
discovery with recent external information; without it discovery uses local
probes and baseline heuristics. Run `dagger refresh` to refresh the
catalog immediately, or use the refresh button in the registry.

Create a goal on the board or run `dagger run "your goal"`. Milestones
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
