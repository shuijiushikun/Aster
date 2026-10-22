"""换货申请的纯规则；申请时间和订单事实由调用方提供，绝不自动批准。"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class ExchangePolicyResult:
    status: str
    code: str | None = None
    missing_fields: list[str] = field(default_factory=list)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def evaluate_exchange(
    order,
    *,
    customer_id: str,
    submitted_at: datetime,
    window_days: int,
    reason_type: str | None,
    description: str,
    requested_change: str | None,
) -> ExchangePolicyResult:
    if order is None or order.customer_id != customer_id:
        return ExchangePolicyResult("REJECTED", "NOT_FOUND_OR_NOT_OWNED")
    if order.status != "delivered" or order.delivered_date is None:
        return ExchangePolicyResult("REJECTED", "ORDER_NOT_DELIVERED")
    if order.refunded:
        return ExchangePolicyResult("REJECTED", "ORDER_REFUNDED")
    if order.is_final_sale:
        return ExchangePolicyResult("REJECTED", "FINAL_SALE")
    age_days = (_aware(submitted_at) - _aware(order.delivered_date)).days
    if age_days < 0:
        return ExchangePolicyResult("REJECTED", "ORDER_NOT_DELIVERED")
    if age_days > window_days:
        return ExchangePolicyResult("REJECTED", "EXCHANGE_WINDOW_EXPIRED")
    if reason_type is not None and reason_type not in ("size", "defect"):
        return ExchangePolicyResult("REJECTED", "INVALID_REASON")
    fields = {
        "reason_type": reason_type,
        "description": description,
        "requested_change": requested_change,
    }
    missing = [name for name, value in fields.items() if not value or not value.strip()]
    if missing:
        return ExchangePolicyResult("WAITING_INFO", missing_fields=missing)
    return ExchangePolicyResult("PENDING_REVIEW")
