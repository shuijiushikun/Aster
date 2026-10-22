"""One long-lived Postgres Saver and connection pool per backend instance."""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.workflow.graph import compile_workflow


@dataclass
class WorkflowRuntime:
    graph: object
    saver: AsyncPostgresSaver


_runtime: WorkflowRuntime | None = None


def get_runtime():
    return _runtime


@asynccontextmanager
async def open_workflow(settings=None):
    settings = settings or get_settings()
    url = make_url(settings.database_url).set(drivername="postgresql")
    if sys.platform == "win32" and isinstance(
        asyncio.get_running_loop(), asyncio.ProactorEventLoop
    ):
        raise RuntimeError(
            "Postgres workflow requires SelectorEventLoop on Windows; run python -m scripts.serve"
        )
    async with AsyncConnectionPool(
        conninfo=url.render_as_string(hide_password=False),
        open=False,
        min_size=1,
        max_size=settings.workflow_pool_size,
        timeout=10,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    ) as pool:
        await pool.wait(timeout=10)
        # Serialize SDK migrations across concurrently starting app instances.
        # autocommit is required by the Saver's own CREATE INDEX migrations.
        async with pool.connection() as connection:
            await connection.execute("SELECT pg_advisory_lock(712049301)")
            try:
                await AsyncPostgresSaver(connection).setup()
            finally:
                await connection.execute("SELECT pg_advisory_unlock(712049301)")
        saver = AsyncPostgresSaver(pool, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        yield WorkflowRuntime(compile_workflow(saver), saver)


@asynccontextmanager
async def workflow_lifespan():
    global _runtime
    settings = get_settings()
    if settings.async_database_url.startswith("postgresql"):
        async with open_workflow(settings) as runtime:
            _runtime = runtime
            try:
                yield
            finally:
                _runtime = None
    else:
        # SQLite is still usable for legacy chat; durable workflows require PG.
        yield
