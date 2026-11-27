"""Command-line entry point for the local control plane.

`dagger` with no subcommand works like `claude` or `agy`: run it inside any repository
and it starts DAGGER for that repository, serves the UI and opens it in a browser.
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

import typer
import uvicorn
from click.core import ParameterSource

from dagger import paths

app = typer.Typer(
    help="DAGGER: run it inside a repository to orchestrate agents on that repository.",
    no_args_is_help=False,
)

_DEFAULT_PORT = 8420
_PORT_SEARCH = 20


def _select_project(project: Path | None) -> Path:
    """Resolve the project once and pin it in the environment for the server process."""
    try:
        root = paths.resolve_project(project)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--project") from exc
    os.environ[paths.ENV_PROJECT] = str(root)
    return root


def _port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _pick_port(host: str, preferred: int, explicit: bool) -> int:
    if explicit or _port_free(host, preferred):
        return preferred
    # Another project's DAGGER may hold the default port; take the next free one.
    for candidate in range(preferred + 1, preferred + _PORT_SEARCH):
        if _port_free(host, candidate):
            return candidate
    raise typer.BadParameter(f"no free port in {preferred}-{preferred + _PORT_SEARCH - 1}", param_hint="--port")


def _launch(project: Path, host: str, port: int, open_browser: bool) -> None:
    url = f"http://{'127.0.0.1' if host in ('0.0.0.0', '::') else host}:{port}"
    ui_built = (paths.frontend_dist_dir() / "index.html").is_file()
    typer.echo(f"DAGGER  project: {project}")
    typer.echo(f"        state:   {paths.project_state_dir(project)}")
    if ui_built:
        typer.echo(f"        ui:      {url}")
    else:
        typer.echo(f"        api:     {url}  (UI not built: run `npm run build` in {paths.install_root() / 'frontend'})")
    if open_browser and ui_built:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run("dagger.server:app", host=host, port=port)


def _run_step(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run an external command, failing cleanly on OS errors."""
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        typer.echo(f"Error running {' '.join(cmd)}: {exc}", err=True)
        raise typer.Exit(1) from exc


def _command_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    return detail or f"exited with status {result.returncode}"


def _run_self_update(
    *,
    skip_pull: bool = False,
    reinstall: bool = False,
    no_frontend: bool = False,
) -> None:
    """Update DAGGER in place (editable git checkout or uv tool upgrade)."""
    root = paths.install_root()

    if not (root / ".git").exists():
        uv = shutil.which("uv")
        if uv is None:
            typer.echo(
                "Error: DAGGER is not running from a git checkout and `uv` was not found on PATH.\n"
                "Upgrade manually with: uv tool upgrade dagger-control-plane "
                "(or pip install --upgrade dagger-control-plane)",
                err=True,
            )
            raise typer.Exit(1)
        cmd = [uv, "tool", "upgrade", "dagger-control-plane"]
        typer.echo(f"Upgrading DAGGER via uv: {' '.join(cmd)}")
        result = _run_step(cmd)
        if result.returncode != 0:
            typer.echo(f"Error: uv tool upgrade failed: {_command_error(result)}", err=True)
            raise typer.Exit(1)
        typer.echo("DAGGER upgraded. Restart any running `dagger` server to pick up the updated version.")
        return

    steps_run: list[str] = []
    changed_files: set[str] = set()

    if not skip_pull:
        git = shutil.which("git")
        if git is None:
            typer.echo(
                "Error: `git` not found on PATH; cannot pull updates (re-run with `dagger update --skip-pull`).",
                err=True,
            )
            raise typer.Exit(1)

        status = _run_step([git, "-C", str(root), "status", "--porcelain"])
        if status.returncode != 0:
            typer.echo(f"Error checking git status in {root}: {_command_error(status)}", err=True)
            raise typer.Exit(1)
        if status.stdout.strip():
            typer.echo(
                f"Error: DAGGER checkout at {root} has uncommitted changes. "
                "Commit or stash them first, or re-run with `dagger update --skip-pull`.",
                err=True,
            )
            raise typer.Exit(1)

        branch = _run_step([git, "-C", str(root), "symbolic-ref", "--short", "-q", "HEAD"])
        if branch.returncode != 0 or not branch.stdout.strip():
            typer.echo(
                f"Error: DAGGER checkout at {root} is in a detached HEAD state. "
                f"Check out a branch (e.g. `git -C {root} checkout main`) or re-run with `dagger update --skip-pull`.",
                err=True,
            )
            raise typer.Exit(1)

        old_rev = _run_step([git, "-C", str(root), "rev-parse", "--short", "HEAD"])
        if old_rev.returncode != 0 or not old_rev.stdout.strip():
            typer.echo(f"Error reading current commit in {root}: {_command_error(old_rev)}", err=True)
            raise typer.Exit(1)
        old_sha = old_rev.stdout.strip()

        typer.echo(f"Pulling latest DAGGER updates in {root} ({branch.stdout.strip()} @ {old_sha})...")
        pull = _run_step([git, "-C", str(root), "pull", "--ff-only"])
        if pull.returncode != 0:
            typer.echo(f"Error: git pull --ff-only failed: {_command_error(pull)}", err=True)
            raise typer.Exit(1)

        new_rev = _run_step([git, "-C", str(root), "rev-parse", "--short", "HEAD"])
        if new_rev.returncode != 0 or not new_rev.stdout.strip():
            typer.echo(f"Error reading updated commit in {root}: {_command_error(new_rev)}", err=True)
            raise typer.Exit(1)
        new_sha = new_rev.stdout.strip()

        if new_sha != old_sha:
            diff = _run_step([git, "-C", str(root), "diff", "--name-only", old_sha, new_sha])
            if diff.returncode == 0:
                changed_files = {line.strip() for line in diff.stdout.splitlines() if line.strip()}
            typer.echo(f"Updated checkout: {old_sha} -> {new_sha}")
            steps_run.append(f"git pull ({old_sha} -> {new_sha})")
        else:
            typer.echo(f"Checkout already up to date ({new_sha}).")
    else:
        typer.echo(f"Skipping git pull for {root} (--skip-pull).")

    deps_changed = any(path in {"pyproject.toml", "uv.lock"} for path in changed_files)
    if reinstall or deps_changed:
        uv = shutil.which("uv")
        if uv is not None:
            install_cmd = [uv, "tool", "install", "--editable", str(root), "--reinstall"]
        else:
            install_cmd = [sys.executable, "-m", "pip", "install", "-e", str(root)]
        typer.echo(f"Refreshing Python install: {' '.join(install_cmd)}")
        install_res = _run_step(install_cmd)
        if install_res.returncode != 0:
            typer.echo(f"Error: Python install refresh failed: {_command_error(install_res)}", err=True)
            raise typer.Exit(1)
        steps_run.append("python reinstall")

    frontend_dir = root / "frontend"
    if not no_frontend and (frontend_dir / "package.json").is_file():
        frontend_changed = any(path.startswith("frontend/") for path in changed_files)
        lock_changed = "frontend/package-lock.json" in changed_files
        node_modules_missing = not (frontend_dir / "node_modules").is_dir()
        dist_missing = not (frontend_dir / "dist" / "index.html").is_file()
        need_npm_ci = node_modules_missing or lock_changed
        need_build = frontend_changed or dist_missing or skip_pull or reinstall or need_npm_ci

        if need_npm_ci or need_build:
            npm = shutil.which("npm")
            if npm is None:
                typer.echo(
                    "Error: `npm` not found on PATH; cannot rebuild frontend UI (use `--no-frontend` to skip).",
                    err=True,
                )
                raise typer.Exit(1)
            if need_npm_ci:
                typer.echo(f"Installing frontend dependencies in {frontend_dir}...")
                ci_res = _run_step([npm, "ci"], cwd=frontend_dir)
                if ci_res.returncode != 0:
                    typer.echo(f"Error: npm ci failed: {_command_error(ci_res)}", err=True)
                    raise typer.Exit(1)
                steps_run.append("npm ci")
            if need_build:
                typer.echo(f"Building frontend UI in {frontend_dir}...")
                build_res = _run_step([npm, "run", "build"], cwd=frontend_dir)
                if build_res.returncode != 0:
                    typer.echo(f"Error: npm run build failed: {_command_error(build_res)}", err=True)
                    raise typer.Exit(1)
                steps_run.append("frontend build")

    if steps_run:
        typer.echo(f"Update complete ({', '.join(steps_run)}).")
    else:
        typer.echo("DAGGER is already up to date; no rebuild steps needed.")
    typer.echo("Restart any running `dagger` server to pick up the updated version.")


def _update_flag_callback(ctx: typer.Context, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    _run_self_update()
    raise typer.Exit(0)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    project: Path | None = typer.Option(
        None, "--project", "-C", help="Repository to work on (default: git root of the current directory)."
    ),
    host: str = typer.Option("127.0.0.1", help="Interface to bind; defaults to loopback."),
    port: int = typer.Option(_DEFAULT_PORT, min=1, max=65535, help="HTTP port (next free one if taken)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the UI in a browser."),
    update: bool = typer.Option(
        False,
        "--update",
        is_eager=True,
        callback=_update_flag_callback,
        help="Update DAGGER in place (pull, reinstall if needed, rebuild UI) and exit.",
    ),
) -> None:
    """Start DAGGER for the current repository and open its UI."""
    if ctx.invoked_subcommand == "update":
        return
    root = _select_project(project)
    if ctx.invoked_subcommand is not None:
        return
    port_explicit = ctx.get_parameter_source("port") not in (None, ParameterSource.DEFAULT)
    _launch(root, host, _pick_port(host, port, port_explicit), open_browser=not no_browser)


@app.command("update")
def update_cmd(
    skip_pull: bool = typer.Option(False, "--skip-pull", help="Skip `git pull` (useful with local edits)."),
    reinstall: bool = typer.Option(False, "--reinstall", help="Force refreshing the Python editable install."),
    no_frontend: bool = typer.Option(False, "--no-frontend", help="Skip rebuilding the frontend UI."),
) -> None:
    """Update DAGGER in place from any directory."""
    _run_self_update(skip_pull=skip_pull, reinstall=reinstall, no_frontend=no_frontend)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind; defaults to loopback."),
    port: int = typer.Option(_DEFAULT_PORT, min=1, max=65535, help="HTTP port."),
) -> None:
    """Start the HTTP and WebSocket server without opening a browser."""
    uvicorn.run("dagger.server:app", host=host, port=port)


@app.command()
def info() -> None:
    """Show which project DAGGER would use and where it keeps state."""
    project = paths.resolve_project()
    typer.echo(f"project:     {project}")
    typer.echo(f"state:       {paths.project_state_dir(project)}")
    typer.echo(f"containers:  {paths.writable_containers_dir(project)}")
    typer.echo(f"install:     {paths.install_root()}")
    typer.echo(f"ui built:    {(paths.frontend_dist_dir() / 'index.html').is_file()}")


async def _refresh() -> None:
    from dagger.agents.generator import AgentGenerator
    from dagger.db import DatabaseManager

    project = paths.resolve_project()
    data = paths.project_state_dir(project)
    data.mkdir(parents=True, exist_ok=True)
    db = DatabaseManager(data / "dagger.sqlite3")
    await db.initialize()
    generator = AgentGenerator(
        db, export_dir=paths.writable_containers_dir(project), job_store_path=data / "scheduler.sqlite3"
    )
    try:
        containers = await generator.refresh_catalog()
        typer.echo("ID\tNAME\tPROVIDER\tACTIVE")
        for item in containers:
            typer.echo(f"{item.container_id}\t{item.name}\t{item.provider}\t{item.active}")
    finally:
        await generator.aclose()
        await db.close()


@app.command()
def refresh() -> None:
    """Refresh the agent and capability catalog."""
    asyncio.run(_refresh())


async def _run(goal: str) -> None:
    from uuid import uuid4

    from dagger.schema import KanbanTask
    from dagger.server import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        run_id = str(uuid4())
        task = KanbanTask(title=goal, description=goal, status="Running", run_id=run_id)
        await application.state.db.upsert_task(task)
        state = await application.state.orchestrator.start(run_id, goal, kanban_id=task.task_id)
        typer.echo(json.dumps({"task_id": task.task_id, "run_id": run_id, "state": state}, default=str))


@app.command()
def run(
    goal: str = typer.Argument(..., help="Goal to execute."),
) -> None:
    """Create and start a Kanban task for the current repository."""
    asyncio.run(_run(goal))


if __name__ == "__main__":
    app()
