"""将客户明确输入的订单尾号匹配到本人订单；不使用模型生成的订单号。"""

import re

from sqlalchemy import func, select

from app.db.models import Order


async def resolve_order_reference(session, customer_id: str, text: str, *, previous_text: str | None = None) -> str | None:
    full = {value.lower() for value in re.findall(
        r"(?<![A-Za-z0-9_-])ORD-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*(?![A-Za-z0-9_-])",
        text, re.IGNORECASE,
    )}
    if full:
        if len(full) != 1:
            return None
        condition = func.lower(Order.id) == next(iter(full)).lower()
    else:
        # 四位以上的独立尾号；金额、小数、日期和多个订单的组合不做推测。
        suffixes = set(re.findall(
            r"(?<![A-Za-z0-9_./:$-])([0-9]{4,})(?![A-Za-z0-9_./:-]|美元|元|块|年|月|日|小时)", text,
        ))
        if not suffixes and previous_text:
            return await resolve_order_reference(session, customer_id, previous_text)
        if len(suffixes) != 1:
            return None
        condition = Order.id.endswith("-" + next(iter(suffixes)))
    rows = list(await session.scalars(select(Order.id).where(
        Order.customer_id == customer_id, condition,
    ).limit(2)))
    return rows[0] if len(rows) == 1 else None
