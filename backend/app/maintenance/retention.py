"""数据保留任务：删除超过 RETENTION_DAYS 天的会话。

聊天记录包含个人数据，不应无限期保留。本任务可作为 Kubernetes CronJob
运行，也可手动执行：
`python -m app.maintenance.retention [--dry-run]`。

采用分批删除，避免积压数据过多时长时间持锁。退款记录作为财务记录保留；
会话删除后，退款记录中原有的会话 ID 将不再对应在线会话，这是预期行为。

`--demo-hours N` 还会清理超过 N 小时的公开演示沙箱（客户 ID 以 `DM-` 开头，
见 `app/demo.py`），包括其订单和退款，因为它们都是模拟数据。每个沙箱的会话
会先在同一事务内复制到 `conversation_archive`，并进行违规核验
（见 `app/maintenance/archive.py`）；归档记录自身也会在 RETENTION_DAYS 天后删除。
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from app.config import get_settings
from app.db import session as db
from app.db.models import (
    Conversation,
    ConversationArchive,
    Customer,
    Message,
    Order,
    ReasoningEvent,
    Refund,
)
from app.demo import DEMO_PREFIX
from app.maintenance.archive import archive

BATCH = 500


async def purge(days: int, dry_run: bool = False) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    total = 0
    while True:
        async with db.SessionLocal() as session:
            ids = (
                await session.scalars(
                    select(Conversation.id).where(Conversation.created_at < cutoff).limit(BATCH)
                )
            ).all()
            if not ids:
                break
            total += len(ids)
            if dry_run:
                break
            await session.execute(delete(ReasoningEvent).where(ReasoningEvent.conversation_id.in_(ids)))
            await session.execute(delete(Message).where(Message.conversation_id.in_(ids)))
            await session.execute(delete(Conversation).where(Conversation.id.in_(ids)))
            await session.commit()
    return total


async def purge_archive(days: int, dry_run: bool = False) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with db.SessionLocal() as session:
        ids = (
            await session.scalars(
                select(ConversationArchive.conversation_id).where(
                    ConversationArchive.archived_at < cutoff
                )
            )
        ).all()
        if ids and not dry_run:
            await session.execute(
                delete(ConversationArchive).where(ConversationArchive.archived_at < cutoff)
            )
            await session.commit()
    return len(ids)


async def purge_demo(hours: int, dry_run: bool = False) -> tuple[int, int, int]:
    """先归档，再删除超过 `hours` 小时的演示沙箱。

    返回（清理的沙箱数、归档的会话数、发现违规的会话数）。
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    total = archived = breached = 0
    while True:
        async with db.SessionLocal() as session:
            cids = (
                await session.scalars(
                    select(Customer.id)
                    .where(Customer.id.like(f"{DEMO_PREFIX}%"), Customer.created_at < cutoff)
                    .limit(BATCH)
                )
            ).all()
            if not cids:
                break
            total += len(cids)
            if dry_run:
                break
            records = await archive(session, list(cids))
            archived += len(records)
            breached += sum(1 for r in records if r["breach"])
            convs = select(Conversation.id).where(Conversation.customer_id.in_(cids))
            orders = select(Order.id).where(Order.customer_id.in_(cids))
            await session.execute(delete(ReasoningEvent).where(ReasoningEvent.conversation_id.in_(convs)))
            await session.execute(delete(Message).where(Message.conversation_id.in_(convs)))
            await session.execute(delete(Refund).where(Refund.order_id.in_(orders)))
            await session.execute(delete(Conversation).where(Conversation.customer_id.in_(cids)))
            await session.execute(delete(Order).where(Order.customer_id.in_(cids)))
            await session.execute(delete(Customer).where(Customer.id.in_(cids)))
            await session.commit()
    return total, archived, breached


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=settings.retention_days)
    parser.add_argument(
        "--demo-hours",
        type=int,
        default=settings.demo_data_ttl_hours if settings.is_demo else 0,
        help="also purge demo sandboxes older than this (0 = skip)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    async def run() -> tuple[int, tuple[int, int, int], int]:
        demo = await purge_demo(args.demo_hours, args.dry_run) if args.demo_hours else (0, 0, 0)
        convs = await purge(args.days, args.dry_run)
        archived = await purge_archive(args.days, args.dry_run)
        await db.dispose()
        return convs, demo, archived

    convs, (sandboxes, archived, breached), expired = asyncio.run(run())
    verb = "would delete" if args.dry_run else "deleted"
    print(f"retention: {verb} {convs} conversations older than {args.days} days")
    if args.demo_hours:
        print(
            f"retention: {verb} {sandboxes} demo sandboxes older than {args.demo_hours} hours "
            f"(archived {archived} conversations, {breached} with a breach)"
        )
    print(f"retention: {verb} {expired} archived conversations older than {args.days} days")


if __name__ == "__main__":
    main()
