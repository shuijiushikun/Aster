"""压力测试数据初始化和公开演示共用的场景订单集。

每个以此方式创建的客户，都会获得覆盖各个退款规则分支的订单，
使同一客户身份能够完整体验批准、拒绝和转人工流程。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from app.db.models import Order, utcnow
from app.localization import product_name


@dataclass(frozen=True)
class Scenario:
    suffix: str
    key: str
    product: str
    amount: tuple[float, float]
    delivered_days: tuple[int, int]
    final_sale: bool = False
    refunded: bool = False


# 订单 ID 后缀与其覆盖的退款规则分支之间的对应关系。
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("A", "refundable", "Noise-Cancelling Headphones", (40, 400), (2, 20)),
    Scenario("B", "final_sale", "Clearance Hoodie", (15, 80), (2, 20), final_sale=True),
    Scenario("C", "high_value", "Ultrabook Laptop", (600, 2000), (2, 20)),
    Scenario("D", "out_of_window", "Desk Lamp", (20, 300), (40, 90)),
    Scenario("E", "already_refunded", "Mechanical Keyboard", (20, 300), (2, 20), refunded=True),
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
                product_name=product_name(s.product),
                category="general",
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
