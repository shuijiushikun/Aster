"""复用当前用户消息 ID 隔离轮次；版本校验与写入共用短事务中的会话行锁。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from sqlalchemy import Connection, event, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.db.models import Conversation, Message


class StaleTurn(RuntimeError):
    def __init__(self):
        super().__init__("本轮处理已失效，请以最新一轮对话和订单状态为准。")


@dataclass(frozen=True)
class TurnStamp:
    conversation_id: str
    message_id: int


_turn: ContextVar[TurnStamp | None] = ContextVar("database_turn", default=None)


def current_turn() -> TurnStamp | None:
    return _turn.get()


@contextmanager
def turn_scope(stamp: TurnStamp | None):
    token = _turn.set(stamp)
    try:
        yield
    finally:
        _turn.reset(token)


class TurnSession(Session):
    """应用统一的会话工厂使用此类；普通接口和后台任务没有轮次上下文。"""


@event.listens_for(TurnSession, "after_begin")
def check_turn(session: Session, transaction: SessionTransaction, connection: Connection) -> None:
    if transaction.nested:
        return
    session.info["turn_guard_locked"] = False
    stamp = current_turn()
    if stamp is None:
        return
    if connection.dialect.name == "sqlite":
        # SQLite 忽略 FOR UPDATE；显式写事务覆盖版本检查和后续写入。
        connection.exec_driver_sql("BEGIN IMMEDIATE")
    elif connection.dialect.name == "postgresql":
        connection.exec_driver_sql("SET LOCAL lock_timeout = '5s'")
    exists = connection.execute(select(Conversation.id).where(
        Conversation.id == stamp.conversation_id).with_for_update()).scalar_one_or_none()
    # 先拿锁再读最新消息，避免等待行锁前的快照漏掉刚提交的新轮次。
    latest = connection.execute(select(func.max(Message.id)).where(
        Message.conversation_id == stamp.conversation_id, Message.role == "user")).scalar_one()
    if exists is None or latest != stamp.message_id:
        raise StaleTurn()
    session.info["turn_guard_locked"] = True


async def lock_conversation(session: AsyncSession, cid: str, lock_token: str | None = None) -> None:
    """接手消息和用户确认先锁会话，再核对 Redis 所有权；旧锁不能创建新版本。"""
    from app import redis as shared
    from app.after_sales.locking import begin_order_write

    await begin_order_write(session)
    if session.get_bind().dialect.name == "postgresql":
        await session.execute(text("SET LOCAL lock_timeout = '5s'"))
    conversation = await session.scalar(select(Conversation.id).where(
        Conversation.id == cid).with_for_update())
    if conversation is None or (lock_token is not None and not await shared.owns_turn_lock(cid, lock_token)):
        raise StaleTurn()
