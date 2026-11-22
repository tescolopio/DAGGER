"""Command-line entry point for the local control plane."""

import asyncio
import json
import os
from pathlib import Path

import typer
import uvicorn

app = typer.Typer(help="Run the DAGGER control plane.")


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Interface to bind; defaults to loopback."),
    port: int = typer.Option(8420, min=1, max=65535, help="HTTP port."),
) -> None:
    """Start the HTTP and WebSocket server."""
    uvicorn.run("dagger.server:app", host=host, port=port)


async def _refresh() -> None:
    from dagger.agents.generator import AgentGenerator
    from dagger.db import DatabaseManager

    data = Path(os.environ.get("DAGGER_DATA_DIR", "data"))
    db = DatabaseManager(data / "dagger.sqlite3")
    await db.initialize()
    generator = AgentGenerator(
        db, export_dir="containers", job_store_path=data / "scheduler.sqlite3"
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
    """Create and start a Kanban task."""
    asyncio.run(_run(goal))


if __name__ == "__main__":
    app()
