"""统一写入边界：PostgreSQL按订单行加锁，SQLite开发模式先取得写锁。"""

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Order


async def begin_order_write(session: AsyncSession) -> None:
    # SQLite忽略FOR UPDATE；在读取业务事实之前开始写事务，避免退款/受理各自读到旧快照。
    # PostgreSQL由下面的行锁串行化同订单操作，不锁住其他订单。
    if session.get_bind().dialect.name == "sqlite":
        await session.connection()
        if not session.info.get("turn_guard_locked"):
            await session.execute(text("BEGIN IMMEDIATE"))


async def lock_order(session: AsyncSession, order_id: str) -> Order | None:
    return await session.scalar(select(Order).where(Order.id == order_id).with_for_update())
