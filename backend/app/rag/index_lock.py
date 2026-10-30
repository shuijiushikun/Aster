"""管理端索引任务互斥；不使用可能到期的 Redis 租约。"""

import hashlib
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy import text

from app.db import session as db


@asynccontextmanager
async def index_lock(settings):
    digest = hashlib.sha256(
        f"aster-index:{settings.qdrant_url.rstrip('/')}:{settings.qdrant_collection}".encode()).digest()
    if db.engine.dialect.name == "postgresql":
        # 专用连接持有会话级锁；Embedding 等待期间没有打开数据库事务。
        async with db.engine.connect() as connection:
            await connection.execution_options(isolation_level="AUTOCOMMIT")
            try:
                acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"),
                                                   {"key": int.from_bytes(digest[:8], signed=True)})
                if not acquired:
                    raise RuntimeError("index task already running")
                pid = await connection.scalar(text("SELECT pg_backend_pid()"))

                async def verify():
                    if await connection.scalar(text("SELECT pg_backend_pid()")) != pid:
                        raise RuntimeError("index lock connection lost")

                yield verify
            finally:
                # 物理断开，避免连接池归还时留下会话级 advisory lock。
                await connection.invalidate()
        return

    # SQLite 本地开发使用 OS 文件锁；进程退出后自动释放，支持多进程。
    path = Path(tempfile.gettempdir()) / f"aster-index-{digest.hex()}.lock"
    with path.open("a+b") as handle:
        if not path.stat().st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            def lock():
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            def lock():
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            lock()
        except OSError:
            raise RuntimeError("index task already running") from None

        async def verify():
            pass  # 文件描述符保持打开，OS 锁没有 TTL。

        yield verify
