import os

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app import redis as shared
from app.auth import ROLE_ADMIN, mint_token
from app.db import seed
from app.db import session as db
from app.db.models import Base

# Opt-in: run the suite against real Postgres/Redis (CI sets these).
TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", "")
TEST_REDIS_URL = os.getenv("TEST_REDIS_URL", "")


@pytest.fixture
async def engine(tmp_path):
    """An isolated, seeded database per test (SQLite file, or Postgres in CI)."""
    if TEST_DATABASE_URL:
        from app.config import _async_db_url

        eng = create_async_engine(_async_db_url(TEST_DATABASE_URL))
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        eng = db.make_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    previous = db.engine
    db.configure(eng)
    async with db.SessionLocal() as s:
        seed._load(s)
        await s.commit()
    yield eng
    db.configure(previous)
    await eng.dispose()


@pytest.fixture(autouse=True)
def reset_primary_breaker():
    """The model circuit breaker is per-process state; start each test closed."""
    from app.agent import graph

    graph.breaker.open_until = 0.0
    yield
    graph.breaker.open_until = 0.0


@pytest.fixture(autouse=True)
async def redis_client():
    """Fresh Redis state per test (fakeredis, or a real Redis in CI)."""
    if TEST_REDIS_URL:
        from redis.asyncio import Redis

        client = Redis.from_url(TEST_REDIS_URL, decode_responses=True)
        await client.flushdb()
    else:
        from fakeredis import FakeAsyncRedis, FakeServer

        client = FakeAsyncRedis(server=FakeServer(), decode_responses=True)
    shared.set_redis(client)
    yield client
    shared.set_redis(None)
    await client.aclose()


@pytest.fixture
async def db_session(engine) -> AsyncSession:
    async with db.SessionLocal() as s:
        yield s


@pytest.fixture
async def client(engine):
    from sse_starlette.sse import AppStatus

    from app.main import app

    # sse-starlette caches an exit Event bound to the first event loop it saw.
    AppStatus.should_exit_event = None

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def auth(customer_id: str) -> dict:
    return {"Authorization": f"Bearer {mint_token(customer_id)}"}


def admin_auth() -> dict:
    return {"Authorization": f"Bearer {mint_token('admin-1', ROLE_ADMIN)}"}


def tools_by_name(tool_list) -> dict:
    return {t.name: t for t in tool_list}
