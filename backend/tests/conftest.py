"""Tests need a throwaway Postgres: export TEST_DATABASE_URL=postgresql://...

The public schema of that database is wiped before every test.
"""
import os

import pytest

from app import db

TEST_DSN = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def pool():
    if not TEST_DSN:
        pytest.skip("TEST_DATABASE_URL not set")
    p = await db.create_pool(TEST_DSN)
    async with p.acquire() as c:
        await c.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await db.migrate(p)
    yield p
    await p.close()
