"""显式清理旧的 15 位退款演示客户，先备份，再在一个事务中删除关联记录。"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.engine import make_url

from app.config import BACKEND_ROOT, get_settings
from app.db import session as db
from app.db.models import (
    AfterSalesTicket,
    AgentToolRequest,
    Conversation,
    ConversationArchive,
    ConversationSummaryRecord,
    Customer,
    ExchangeAcceptance,
    Message,
    Order,
    ReasoningEvent,
    Refund,
    TicketApproval,
    TicketEvent,
    TicketRequest,
)

_NAMES = ["Alice Johnson", "Bob Smith", "Carol Davis", "David Lee", "Emma Wilson",
          "Frank Miller", "Grace Chen", "Henry Brown", "Iris Taylor", "Jack Anderson",
          "Karen White", "Liam Garcia", "Mia Martinez", "Noah Rodriguez", "Olivia Hernandez"]
_EXPECTED = {f"CUST-{i:03d}": (name, name.split()[0].lower() + "@example.com")
             for i, name in enumerate(_NAMES, 1)}


async def cleanup(*, apply=False, backup_dir: Path | None = None):
    async with db.SessionLocal() as session, session.begin():
        customers = list(await session.scalars(select(Customer).where(Customer.id.in_(_EXPECTED))
                                               .with_for_update()))
        if any((c.name, c.email) != _EXPECTED[c.id] for c in customers):
            raise ValueError("旧样本编号下存在不同客户资料，停止清理。")
        ids = [c.id for c in customers]
        orders = select(Order.id).where(Order.customer_id.in_(ids))
        conversations = select(Conversation.id).where(Conversation.customer_id.in_(ids))
        tickets = select(AfterSalesTicket.id).where(AfterSalesTicket.customer_id.in_(ids))
        ticket_ids = list(await session.scalars(tickets))
        if ticket_ids:
            # 旧退款样本正常没有换货；遇到后续创建的工单时保留现场，避免遗留 checkpoint。
            raise ValueError("旧客户已有换货工单，需要同时核对工作流后再清理。")
        related = await session.scalar(select(AfterSalesTicket.id).where(or_(
            AfterSalesTicket.order_id.in_(orders), AfterSalesTicket.origin_conversation_id.in_(conversations),
        )).limit(1))
        if related:
            raise ValueError("存在关联换货工单，停止清理以保留业务关系。")
        targets = [
            (TicketRequest, TicketRequest.ticket_id.in_(ticket_ids)),
            (TicketEvent, TicketEvent.ticket_id.in_(ticket_ids)),
            (TicketApproval, TicketApproval.ticket_id.in_(ticket_ids)),
            (ExchangeAcceptance, ExchangeAcceptance.ticket_id.in_(ticket_ids)),
            (AfterSalesTicket, AfterSalesTicket.id.in_(ticket_ids)),
            (Refund, Refund.order_id.in_(orders)),
            (AgentToolRequest, AgentToolRequest.customer_id.in_(ids)),
            (ConversationSummaryRecord, ConversationSummaryRecord.conversation_id.in_(conversations)),
            (ReasoningEvent, ReasoningEvent.conversation_id.in_(conversations)),
            (Message, Message.conversation_id.in_(conversations)),
            (Conversation, Conversation.id.in_(conversations)),
            (Order, Order.id.in_(orders)),
            (ConversationArchive, ConversationArchive.customer_id.in_(ids)),
            (Customer, Customer.id.in_(ids)),
        ]
        backup, counts = {}, {}
        for model, condition in targets:
            rows = (await session.execute(select(model.__table__).where(condition))).mappings().all()
            backup[model.__tablename__] = [dict(row) for row in rows]
            counts[model.__tablename__] = len(rows)
        if apply and ids:
            if backup_dir is None:
                raise ValueError("删除前必须指定本地备份目录。")
            backup_dir.mkdir(parents=True, exist_ok=True)
            path = backup_dir / ("legacy-demo-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".json")
            with path.open("x", encoding="utf-8") as stream:
                json.dump(backup, stream, ensure_ascii=False, indent=2, default=str)
            # 子查询引用的订单与会话在最后删除，顺序不能调换。
            for model, condition in targets:
                await session.execute(delete(model).where(condition))
            counts["backup"] = str(path)
        return counts


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="备份后删除；省略时仅统计")
    args = parser.parse_args()
    s = get_settings()
    url = make_url(s.database_url)
    if s.env != "dev" or s.auth_mode != "dev" or url.host not in ("localhost", "127.0.0.1"):
        raise ValueError("此脚本仅用于明确配置的本机开发数据库。")
    try:
        print(json.dumps(await cleanup(apply=args.apply, backup_dir=BACKEND_ROOT / "var" / "backups"), ensure_ascii=False))
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
