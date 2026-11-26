"""Command-line entry point for the local control plane.

`dagger` with no subcommand works like `claude` or `agy`: run it inside any repository
and it starts DAGGER for that repository, serves the UI and opens it in a browser.
"""

import asyncio
import json
import os
import socket
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


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    project: Path | None = typer.Option(
        None, "--project", "-C", help="Repository to work on (default: git root of the current directory)."
    ),
    host: str = typer.Option("127.0.0.1", help="Interface to bind; defaults to loopback."),
    port: int = typer.Option(_DEFAULT_PORT, min=1, max=65535, help="HTTP port (next free one if taken)."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open the UI in a browser."),
) -> None:
    """Start DAGGER for the current repository and open its UI."""
    root = _select_project(project)
    if ctx.invoked_subcommand is not None:
        return
    port_explicit = ctx.get_parameter_source("port") not in (None, ParameterSource.DEFAULT)
    _launch(root, host, _pick_port(host, port, port_explicit), open_browser=not no_browser)


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
