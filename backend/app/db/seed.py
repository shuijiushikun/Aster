"""Rebuildable after-sales demo samples; existing business facts are never reset."""

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db import session as db
from app.db.models import (
    AfterSalesTicket,
    Conversation,
    Customer,
    Order,
    Product,
    Refund,
    TicketEvent,
    utcnow,
)

SEED_FILE = Path(__file__).resolve().parent / "data" / "demo_seed.json"


def demo_data() -> dict:
    return json.loads(SEED_FILE.read_text(encoding="utf-8"))


async def ensure_catalog(session) -> int:
    inserted = 0
    for fields in demo_data()["products"]:
        existing = await session.get(Product, fields["id"])
        if existing is None:
            session.add(Product(**fields))
            inserted += 1
        elif existing.model_code != fields["model_code"] or existing.category != fields["category"]:
            raise ValueError("演示商品编号已被不同型号或品类占用，请使用独立演示数据库。")
    await session.flush()
    return inserted


async def seed_demo_samples() -> dict[str, int]:
    """Add a separate namespace, then create example states through domain rules.

    Existing business rows are not reset. Only this seed's unfinished operations
    can continue after interruption; dates and user-modified tickets are kept.
    """
    from app.after_sales.schemas import DecisionInput, ExchangeInput, RequestInput
    from app.after_sales.service import TicketService
    from app.auth import Principal

    data = demo_data()
    inserted = {"products": 0, "customers": 0, "orders": 0, "tickets": 0}
    now = utcnow()
    async with db.SessionLocal() as session, session.begin():
        inserted["products"] = await ensure_catalog(session)
        for fields in data["customers"]:
            if await session.get(Customer, fields["id"]) is None:
                session.add(Customer(**fields))
                inserted["customers"] += 1
        await session.flush()
        for fields in data["orders"]:
            if await session.get(Order, fields["id"]) is not None:
                continue
            product = await session.get(Product, fields["product_id"])
            days = fields["delivered_days_ago"]
            delivered = now - timedelta(days=days) if days is not None else None
            session.add(Order(
                id=fields["id"], customer_id=fields["customer_id"], product_id=product.id,
                product_name=product.name, category=product.category,
                amount=Decimal(fields["amount"]), status=fields.get("status", "delivered"),
                delivered_date=delivered, order_date=(delivered or now) - timedelta(days=3),
                is_final_sale=fields.get("is_final_sale", False), refunded=fields.get("refunded", False),
            ))
            inserted["orders"] += 1
            if fields.get("refunded"):
                await session.flush()
                session.add(Refund(order_id=fields["id"], amount=Decimal(fields["amount"]),
                                   decision="approved", reason="演示样本：历史模拟退款", decided_by="demo-seed"))

    service = TicketService()
    admin = Principal("demo-seed-admin", "admin")
    for fields in data["tickets"]:
        origin = "demo-seed:" + fields["order_id"]
        async with db.SessionLocal() as session, session.begin():
            existing = await session.scalar(select(AfterSalesTicket).where(
                AfterSalesTicket.order_id == fields["order_id"]
            ).order_by(AfterSalesTicket.created_at).limit(1))
            order = await session.get(Order, fields["order_id"])
            customer = Principal(order.customer_id, "customer")
            if existing and existing.origin_conversation_id != origin:
                continue
            if existing:
                events = list(await session.scalars(select(TicketEvent).where(TicketEvent.ticket_id == existing.id)))
                if any(e.kind not in ("CREATED", "DECIDED", "COMPLETED", "CANCELLED")
                       or (e.kind == "DECIDED" and e.actor_id != admin.sub) for e in events):
                    continue
            conversation = await session.get(Conversation, origin)
            if conversation is None:
                session.add(Conversation(id=origin, customer_id=customer.sub))
            elif conversation.customer_id != customer.sub:
                raise ValueError("演示会话编号与客户归属冲突。")
        if existing:
            ticket = existing
        else:
            ticket = await service.create_or_find_exchange(customer, fields["order_id"], ExchangeInput(
                description=fields["description"], reason_type=fields.get("reason_type"),
                requested_change=fields.get("requested_change"),
            ), origin_conversation_id=origin)
            inserted["tickets"] += 1
        target = fields["status"]
        request_id = "demo-seed:" + fields["order_id"]
        if target == "CANCELLED" and ticket.status == "PENDING_REVIEW":
            await service.cancel(customer, ticket.id, RequestInput(request_id=request_id))
        elif target in ("REJECTED", "COMPLETED") and ticket.status in ("PENDING_REVIEW", "APPROVED"):
            if ticket.status == "APPROVED" and target != "COMPLETED":
                continue
            await service.decide(admin, ticket.id, DecisionInput(
                request_id=request_id, decision="approved" if target == "COMPLETED" else "rejected",
                comment="演示样本：按当前业务规则生成的模拟审批。",
            ))
            if target == "COMPLETED":
                await service.accept_exchange(ticket.id)
    return inserted


async def seed_if_empty(create_tables: bool = False) -> bool:
    if create_tables:
        await db.init_db()
    async with db.SessionLocal() as session:
        if await session.scalar(select(Customer.id).limit(1)) is not None:
            return False
    await seed_demo_samples()
    return True


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="添加独立售后演示样本，不清空或改写已有业务数据。")
    parser.add_argument("--demo-samples", action="store_true", help="显式增量添加演示客户、订单与工单")
    args = parser.parse_args()
    try:
        if args.demo_samples:
            print(json.dumps(await seed_demo_samples(), ensure_ascii=False))
        else:
            print("已初始化售后演示数据。" if await seed_if_empty() else "已有客户数据，未自动添加；可使用 --demo-samples。")
    finally:
        await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
