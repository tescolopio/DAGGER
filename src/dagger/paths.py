"""Where DAGGER finds the project it works on, its own bundled assets, and per-project state.

Running `dagger` inside any repository makes that repository the project. State for it
lives under ~/.dagger/projects/<name>-<hash>/ so the project tree stays clean, while the
agent catalog (~/.dagger/containers) is shared by every project.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

# Environment overrides. The CLI sets DAGGER_PROJECT so the server process (started by
# uvicorn from an import string) resolves the same project.
ENV_PROJECT = "DAGGER_PROJECT"
ENV_HOME = "DAGGER_HOME"
ENV_DATA_DIR = "DAGGER_DATA_DIR"
ENV_CONTAINERS_DIR = "DAGGER_CONTAINERS_DIR"


def install_root() -> Path:
    """The DAGGER checkout this package runs from (requires an editable install)."""
    return Path(__file__).resolve().parents[2]


def dagger_home() -> Path:
    override = os.environ.get(ENV_HOME, "").strip()
    return Path(override).expanduser() if override else Path.home() / ".dagger"


def _git_toplevel(start: Path) -> Path | None:
    git = shutil.which("git")
    if git is None:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(start), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    top = result.stdout.strip()
    return Path(top).resolve() if result.returncode == 0 and top else None


def resolve_project(start: str | Path | None = None) -> Path:
    """The project to work on: DAGGER_PROJECT, else the git root of `start`/cwd, else `start`/cwd."""
    override = os.environ.get(ENV_PROJECT, "").strip()
    base = Path(start or override or Path.cwd()).expanduser().resolve()
    if not base.is_dir():
        raise ValueError(f"project directory does not exist: {base}")
    if start is None and override:
        return base
    return _git_toplevel(base) or base


def _project_slug(project: Path) -> str:
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", project.name).strip("-") or "project"
    digest = hashlib.sha256(str(project).encode("utf-8")).hexdigest()[:8]
    return f"{name}-{digest}"


def project_state_dir(project: Path) -> Path:
    """Board, checkpoints, scheduler and file bus for one project.

    DAGGER_DATA_DIR wins. Running inside the DAGGER checkout keeps its legacy ./data
    directory when it already holds a database, so existing history is not orphaned.
    """
    override = os.environ.get(ENV_DATA_DIR, "").strip()
    if override:
        return Path(override).expanduser()
    legacy = project / "data"
    if project == install_root() and (legacy / "dagger.sqlite3").exists():
        return legacy
    return dagger_home() / "projects" / _project_slug(project)


def bundled_containers_dir() -> Path:
    return install_root() / "containers"


def user_containers_dir() -> Path:
    """Shared, writable agent catalog: hot-reloaded and where the generator exports to."""
    override = os.environ.get(ENV_CONTAINERS_DIR, "").strip()
    return Path(override).expanduser() if override else dagger_home() / "containers"


def writable_containers_dir(project: Path) -> Path:
    """Hot-reloaded catalog the generator exports into.

    Inside the DAGGER checkout this stays its versioned containers/ directory, as before.
    """
    if not os.environ.get(ENV_CONTAINERS_DIR, "").strip() and project == install_root():
        return bundled_containers_dir()
    return user_containers_dir()


def bundled_skill_roots(project: Path) -> list[Path]:
    """DAGGER's own skills (SkillForge + dagger-*) when it works on another project."""
    return [] if project == install_root() else [install_root()]


def frontend_dist_dir() -> Path:
    return install_root() / "frontend" / "dist"
