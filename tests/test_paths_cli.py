"""Project resolution, per-project state, the bundled UI and the `dagger` launcher."""

from __future__ import annotations

import socket
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from dagger import cli, paths
from dagger.server import create_app


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_resolve_project_climbs_to_git_root(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    (repo / "src" / "deep").mkdir(parents=True)
    monkeypatch.chdir(repo / "src" / "deep")
    assert paths.resolve_project() == repo
    assert paths.resolve_project(repo / "src") == repo


def test_resolve_project_env_override_is_used_as_is(tmp_path: Path, monkeypatch):
    repo = _git_repo(tmp_path / "proj")
    sub = repo / "sub"
    sub.mkdir()
    monkeypatch.setenv(paths.ENV_PROJECT, str(sub))
    assert paths.resolve_project() == sub.resolve()


def test_resolve_project_rejects_missing_directory(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        paths.resolve_project(tmp_path / "nope")


def test_state_lives_under_dagger_home_per_project(tmp_path: Path, isolated_dagger_home: Path):
    a = _git_repo(tmp_path / "a" / "CSGuru")
    b = _git_repo(tmp_path / "b" / "CSGuru")
    state_a, state_b = paths.project_state_dir(a), paths.project_state_dir(b)
    assert state_a.parent == isolated_dagger_home / "projects"
    assert state_a.name.startswith("CSGuru-") and state_a != state_b  # same name, different repos
    assert paths.writable_containers_dir(a) == isolated_dagger_home / "containers"
    assert paths.bundled_skill_roots(a) == [paths.install_root()]


def test_data_dir_override_and_dagger_repo_legacy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv(paths.ENV_DATA_DIR, str(tmp_path / "custom"))
    assert paths.project_state_dir(tmp_path) == tmp_path / "custom"
    monkeypatch.delenv(paths.ENV_DATA_DIR)
    root = paths.install_root()
    # Inside the DAGGER checkout the versioned catalog and bundled skills stay local.
    assert paths.writable_containers_dir(root) == root / "containers"
    assert paths.bundled_skill_roots(root) == []


def test_server_works_on_external_project_and_serves_ui(tmp_path: Path, monkeypatch, isolated_dagger_home: Path):
    project = _git_repo(tmp_path / "target")
    monkeypatch.setenv(paths.ENV_PROJECT, str(project))
    app = create_app()
    with TestClient(app) as client:
        info = client.get("/api/project").json()
        assert info["project"] == str(project)
        assert info["state_dir"].startswith(str(isolated_dagger_home / "projects"))
        assert app.state.orchestrator.worktree == project
        assert app.state.orchestrator.project_manager.workspace == project
        if (paths.frontend_dist_dir() / "index.html").is_file():
            ui = client.get("/")
            assert ui.status_code == 200 and "<html" in ui.text.lower()
        assert client.get("/api/containers").status_code == 200  # API not shadowed by the UI mount
    # Nothing written into the project itself.
    assert sorted(p.name for p in project.iterdir()) == [".git"]


def test_cli_info_reports_project(tmp_path: Path, isolated_dagger_home: Path):
    project = _git_repo(tmp_path / "target")
    result = CliRunner().invoke(cli.app, ["-C", str(project), "info"])
    assert result.exit_code == 0, result.output
    assert f"project:     {project}" in result.output
    assert str(isolated_dagger_home / "projects") in result.output


def test_cli_rejects_missing_project(tmp_path: Path):
    result = CliRunner().invoke(cli.app, ["-C", str(tmp_path / "missing"), "info"])
    assert result.exit_code != 0


def test_bare_dagger_launches_with_next_free_port(tmp_path: Path, monkeypatch):
    project = _git_repo(tmp_path / "target")
    launched = {}
    monkeypatch.setattr(cli, "_launch", lambda root, host, port, open_browser: launched.update(
        root=root, port=port, open_browser=open_browser))
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        taken = busy.getsockname()[1]
        result = CliRunner().invoke(cli.app, ["-C", str(project), "--port", str(taken)])
        assert result.exit_code == 0, result.output
        assert launched["port"] == taken  # an explicit port is honoured as given
        monkeypatch.setattr(cli, "_DEFAULT_PORT", taken)
        assert cli._pick_port("127.0.0.1", taken, explicit=False) != taken
    assert launched["root"] == project and launched["open_browser"] is True
