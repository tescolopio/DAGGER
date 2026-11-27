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


def _fake_checkout(tmp_path: Path, *, with_git: bool = True, with_node_modules: bool = True, with_dist: bool = True) -> Path:
    root = tmp_path / "dagger-checkout"
    root.mkdir(parents=True, exist_ok=True)
    if with_git:
        (root / ".git").mkdir()
    frontend = root / "frontend"
    frontend.mkdir()
    (frontend / "package.json").write_text('{"name": "dagger-frontend"}\n', encoding="utf-8")
    if with_node_modules:
        (frontend / "node_modules").mkdir()
    if with_dist:
        (frontend / "dist").mkdir()
        (frontend / "dist" / "index.html").write_text("<html></html>\n", encoding="utf-8")
    return root


def _completed(cmd: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)


def test_cli_update_flag_runs_from_arbitrary_cwd_without_project(tmp_path: Path, monkeypatch):
    root = _fake_checkout(tmp_path)
    external_dir = tmp_path / "cs-guru"
    external_dir.mkdir()
    monkeypatch.chdir(external_dir)
    monkeypatch.setenv(paths.ENV_PROJECT, str(tmp_path / "nonexistent-project"))
    monkeypatch.setattr(paths, "install_root", lambda: root)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd, cwd=None, **kwargs):
        calls.append((list(cmd), cwd))
        if cmd[-2:] == ["status", "--porcelain"]:
            return _completed(cmd, stdout="")
        if "symbolic-ref" in cmd:
            return _completed(cmd, stdout="main\n")
        if "rev-parse" in cmd:
            return _completed(cmd, stdout="abc1234\n")
        if "pull" in cmd:
            return _completed(cmd, stdout="Already up to date.\n")
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli.app, ["--update"])
    assert result.exit_code == 0, result.output
    assert "already up to date" in result.output.lower()
    assert "Restart any running `dagger` server" in result.output
    assert any("pull" in cmd for cmd, _ in calls)


def test_cli_update_rejects_dirty_working_tree(tmp_path: Path, monkeypatch):
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(paths, "install_root", lambda: root)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, cwd=None, **kwargs):
        if cmd[-2:] == ["status", "--porcelain"]:
            return _completed(cmd, stdout=" M src/dagger/cli.py\n")
        raise AssertionError(f"Should not run further commands on dirty tree: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 1
    assert "uncommitted changes" in result.output
    assert "--skip-pull" in result.output


def test_cli_update_rejects_detached_head(tmp_path: Path, monkeypatch):
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(paths, "install_root", lambda: root)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    def fake_run(cmd, cwd=None, **kwargs):
        if cmd[-2:] == ["status", "--porcelain"]:
            return _completed(cmd, stdout="")
        if "symbolic-ref" in cmd:
            return _completed(cmd, returncode=1, stdout="")
        raise AssertionError(f"Should not run further commands on detached HEAD: {cmd}")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 1
    assert "detached HEAD" in result.output
    assert "--skip-pull" in result.output


def test_cli_update_pull_triggers_reinstall_npm_ci_and_frontend_build(tmp_path: Path, monkeypatch):
    root = _fake_checkout(tmp_path)
    monkeypatch.setattr(paths, "install_root", lambda: root)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")

    calls: list[tuple[list[str], str | None]] = []
    rev_parse_count = 0

    def fake_run(cmd, cwd=None, **kwargs):
        nonlocal rev_parse_count
        calls.append((list(cmd), cwd))
        if cmd[-2:] == ["status", "--porcelain"]:
            return _completed(cmd, stdout="")
        if "symbolic-ref" in cmd:
            return _completed(cmd, stdout="main\n")
        if "rev-parse" in cmd:
            rev_parse_count += 1
            return _completed(cmd, stdout="111aaaa\n" if rev_parse_count == 1 else "222bbbb\n")
        if "pull" in cmd:
            return _completed(cmd, stdout="Fast-forward\n")
        if "diff" in cmd:
            return _completed(
                cmd,
                stdout="pyproject.toml\nfrontend/package-lock.json\nfrontend/src/App.tsx\n",
            )
        return _completed(cmd, stdout="ok\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert "111aaaa -> 222bbbb" in result.output
    assert "Restart any running `dagger` server" in result.output

    expected_uv = ["/usr/bin/uv", "tool", "install", "--editable", str(root), "--reinstall"]
    assert (expected_uv, None) in calls
    assert (["/usr/bin/npm", "ci"], str(root / "frontend")) in calls
    assert (["/usr/bin/npm", "run", "build"], str(root / "frontend")) in calls


def test_cli_update_flags_skip_pull_reinstall_no_frontend(tmp_path: Path, monkeypatch):
    root = _fake_checkout(tmp_path, with_node_modules=False, with_dist=False)
    monkeypatch.setattr(paths, "install_root", lambda: root)
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "uv" else f"/usr/bin/{name}")

    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd, cwd=None, **kwargs):
        calls.append((list(cmd), cwd))
        return _completed(cmd, stdout="ok\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    # --skip-pull --reinstall --no-frontend: no git, pip fallback (uv absent), no npm
    res = CliRunner().invoke(cli.app, ["update", "--skip-pull", "--reinstall", "--no-frontend"])
    assert res.exit_code == 0, res.output
    assert not any("git" in cmd[0] for cmd, _ in calls)
    assert not any("npm" in cmd[0] for cmd, _ in calls)
    assert any(cmd[1:] == ["-m", "pip", "install", "-e", str(root)] for cmd, _ in calls)

    # --skip-pull alone (with missing node_modules/dist): triggers npm ci + npm run build, no git/reinstall
    calls.clear()
    res2 = CliRunner().invoke(cli.app, ["update", "--skip-pull"])
    assert res2.exit_code == 0, res2.output
    assert not any("git" in cmd[0] for cmd, _ in calls)
    assert (["/usr/bin/npm", "ci"], str(root / "frontend")) in calls
    assert (["/usr/bin/npm", "run", "build"], str(root / "frontend")) in calls


def test_cli_update_non_git_install_uses_uv_tool_upgrade(tmp_path: Path, monkeypatch):
    root = _fake_checkout(tmp_path, with_git=False)
    monkeypatch.setattr(paths, "install_root", lambda: root)

    calls: list[list[str]] = []

    def fake_run(cmd, cwd=None, **kwargs):
        calls.append(list(cmd))
        return _completed(cmd, stdout="Upgraded dagger-control-plane\n")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)

    result = CliRunner().invoke(cli.app, ["update"])
    assert result.exit_code == 0, result.output
    assert calls == [["/usr/bin/uv", "tool", "upgrade", "dagger-control-plane"]]
    assert "Restart any running `dagger` server" in result.output

    # Without uv on PATH, fail cleanly with manual upgrade instructions
    calls.clear()
    monkeypatch.setattr(cli.shutil, "which", lambda name: None)
    result_no_uv = CliRunner().invoke(cli.app, ["update"])
    assert result_no_uv.exit_code == 1
    assert "uv tool upgrade dagger-control-plane" in result_no_uv.output
    assert calls == []
