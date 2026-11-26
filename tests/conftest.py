"""Shared isolated database fixtures."""

import pytest
import pytest_asyncio

from dagger.db import DatabaseManager


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "dagger.db"


@pytest_asyncio.fixture
async def db(db_path):
    async with DatabaseManager(db_path, seed_path=False) as database:
        yield database


@pytest.fixture(autouse=True)
def isolated_dagger_home(tmp_path, monkeypatch):
    """Keep tests from reading or writing the real ~/.dagger (shared catalog, project state)."""
    home = tmp_path / "dagger-home"
    monkeypatch.setenv("DAGGER_HOME", str(home))
    monkeypatch.delenv("DAGGER_PROJECT", raising=False)
    monkeypatch.delenv("DAGGER_CONTAINERS_DIR", raising=False)
    return home
