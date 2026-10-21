"""异步数据库引擎和会话工厂。

数据库会话应保持短暂：每个工作单元单独打开会话
（`async with SessionLocal() as s:`），等待模型调用期间不持有会话，
使 Pod 所需的连接池规模取决于数据库操作量，而非打开的聊天流数量。

在调用时通过 `db.SessionLocal()` 查找会话工厂，方便测试替换为隔离的数据库引擎。
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import get_settings
from app.db.models import Base


def make_engine(url: str | None = None) -> AsyncEngine:
    settings = get_settings()
    url = url or settings.async_database_url
    if url.startswith("sqlite"):
        # SQLite 数据库文件所在的目录可能尚未创建。
        if ":///" in url and ":memory:" not in url:
            Path(url.split(":///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        engine = create_async_engine(url, connect_args={"timeout": 30})

        @event.listens_for(engine.sync_engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - 简单的连接初始化。
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    return create_async_engine(
        url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout_s,
        pool_pre_ping=True,
        pool_recycle=1800,
    )


engine: AsyncEngine = make_engine()
SessionLocal = async_sessionmaker(engine, autoflush=False, expire_on_commit=False)


def configure(new_engine: AsyncEngine) -> None:
    """切换应用使用的数据库引擎，供测试和脚本调用。"""
    global engine, SessionLocal
    engine = new_engine
    SessionLocal = async_sessionmaker(new_engine, autoflush=False, expire_on_commit=False)


async def init_db() -> None:
    """直接创建表，仅供开发和测试使用；生产环境使用 Alembic 迁移。"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose() -> None:
    await engine.dispose()
