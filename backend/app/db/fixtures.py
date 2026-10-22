"""公开沙箱订单：复用知识库已有型号，覆盖退款与换货业务。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.db.models import Order, utcnow


@dataclass(frozen=True)
class Scenario:
    suffix: str
    key: str
    product: str
    amount: tuple[float, float]
    delivered_days: tuple[int, int]
    product_id: str
    category: str
    final_sale: bool = False
    refunded: bool = False


# 订单 ID 后缀与其覆盖的退款规则分支之间的对应关系。
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("A", "refundable", "HP-01 无线耳机", (129.99, 129.99), (2, 20), "PRD-HEADPHONE-01", "digital"),
    Scenario("B", "final_sale", "TSHIRT-01 清仓T恤", (19.90, 19.90), (2, 20), "PRD-TSHIRT-01", "apparel", final_sale=True),
    Scenario("C", "high_value", "HP-01 耳机组合装", (699, 699), (2, 20), "PRD-HEADPHONE-01", "digital"),
    Scenario("D", "out_of_window", "TSHIRT-01 基础款T恤", (29.90, 29.90), (40, 90), "PRD-TSHIRT-01", "apparel"),
    Scenario("E", "already_refunded", "HP-01 无线耳机", (129.99, 129.99), (2, 20), "PRD-HEADPHONE-01", "digital", refunded=True),
)

SCENARIO_BY_SUFFIX = {s.suffix: s for s in SCENARIOS}


def scenario_orders(customer_id: str, rng: random.Random) -> list[Order]:
    now = utcnow()
    orders = []
    for s in SCENARIOS:
        delivered = rng.randint(*s.delivered_days)
        orders.append(
            Order(
                id=f"{customer_id}-{s.suffix}",
                customer_id=customer_id,
                product_id=s.product_id,
                product_name=s.product,
                category=s.category,
                amount=Decimal(str(round(rng.uniform(*s.amount), 2))),
                status="delivered",
                order_date=now - timedelta(days=delivered + 3),
                delivered_date=now - timedelta(days=delivered),
                is_final_sale=s.final_sale,
                refunded=s.refunded,
            )
        )
    return orders


def scenario_for(order_id: str) -> str | None:
    """返回预置订单 ID 对应的场景键，例如 `DM-123-A` 对应 refundable（可退款）。"""
    s = SCENARIO_BY_SUFFIX.get(order_id.rsplit("-", 1)[-1])
    return s.key if s else None
