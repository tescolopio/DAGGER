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
