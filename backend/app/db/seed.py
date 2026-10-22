import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from app.db import session as db
from app.db.models import Customer, Order, Product, utcnow
from app.localization import product_name

SEED_FILE = Path(__file__).resolve().parent / "data" / "seed.json"
EXCHANGE_SEED_FILE = SEED_FILE.with_name("exchange_seed.json")


def _days_ago(days: int | None):
    if days is None:
        return None
    return utcnow() - timedelta(days=days)


async def seed_if_empty(create_tables: bool = False) -> bool:
    """仅在需要时加载一次预置数据；实际插入数据时返回 True。

    预置订单日期使用相对于当前时间的天数（days_ago）表示，此处将其转换为
    绝对时间戳，使应用在不同时间启动时，退款期限的边界场景仍然有效。
    """
    if create_tables:
        await db.init_db()
    async with db.SessionLocal() as session:
        existing = await session.scalar(select(Customer).limit(1))
        if existing is not None:
            return False
        _load(session)
        await session.commit()
        return True


def _load(session) -> None:
    """向 `session` 添加预置记录；仅调用 `add`，兼容同步和异步会话。"""
    data = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    for c in data["customers"]:
        session.add(
            Customer(
                id=c["id"],
                name=c["name"],
                email=c["email"],
                loyalty_tier=c.get("loyalty_tier", "standard"),
            )
        )
        for o in c.get("orders", []):
            session.add(
                Order(
                    id=o["id"],
                    customer_id=c["id"],
                    product_name=product_name(o["product_name"]),
                    category=o.get("category", "general"),
                    amount=Decimal(str(o["amount"])),
                    status=o.get("status", "delivered"),
                    order_date=_days_ago(o.get("order_days_ago")),
                    delivered_date=_days_ago(o.get("delivered_days_ago")),
                    is_final_sale=o.get("is_final_sale", False),
                    refunded=o.get("refunded", False),
                )
            )


async def seed_exchange_samples() -> dict[str, int]:
    """显式添加独立换货样本；不重置或改写原客户、订单和退款事实。"""
    data = json.loads(EXCHANGE_SEED_FILE.read_text(encoding="utf-8"))
    inserted = {"products": 0, "customers": 0, "orders": 0}
    async with db.SessionLocal() as session, session.begin():
        for fields in data["products"]:
            if await session.get(Product, fields["id"]) is None:
                session.add(Product(**fields))
                inserted["products"] += 1
        for fields in data["customers"]:
            if await session.get(Customer, fields["id"]) is None:
                session.add(Customer(**fields))
                inserted["customers"] += 1
        await session.flush()
        for fields in data["orders"]:
            if await session.get(Order, fields["id"]) is not None:
                continue
            product = await session.get(Product, fields["product_id"])
            delivered = _days_ago(fields["delivered_days_ago"])
            session.add(Order(id=fields["id"], customer_id=fields["customer_id"], product_id=product.id,
                product_name=product.name, category=product.category, amount=Decimal(fields["amount"]),
                status="delivered", delivered_date=delivered, order_date=delivered-timedelta(days=3),
                is_final_sale=fields["is_final_sale"], refunded=False))
            inserted["orders"] += 1
    return inserted


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Load explicit demo data without resetting existing rows.")
    parser.add_argument("--exchange-samples", action="store_true", help="add the two products and four exchange orders")
    args = parser.parse_args()
    if args.exchange_samples:
        print(json.dumps(asyncio.run(seed_exchange_samples())))
    else:
        inserted = asyncio.run(seed_if_empty())
        print(f"Seeded database from {SEED_FILE}" if inserted else "Database already seeded")
