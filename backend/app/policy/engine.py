"""退款资格的唯一规则来源：模型只能提出请求，工具落库前必须重新调用 evaluate。

纯函数不调用模型、不访问数据库。汉化只改变原因说明，不改变判断顺序和阈值。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from app.config import get_settings
from app.localization import product_name


@dataclass
class PolicyResult:
    eligible: bool
    requires_escalation: bool
    reasons: list[str] = field(default_factory=list)
    refund_amount: float = 0.0

    @property
    def decision(self) -> str:
        """允许写入账本的内部状态值；保留英文以兼容 API 和数据库。"""
        if not self.eligible:
            return "denied"
        if self.requires_escalation:
            return "escalated"
        return "approved"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["decision"] = self.decision
        return d


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _as_aware(dt: datetime | None) -> datetime | None:
    """将 SQLite 返回的无时区时间视为 UTC，以便进行时间比较。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def evaluate(order, customer=None, now: datetime | None = None) -> PolicyResult:
    """按优先级校验：归属、送达、重复退款、清仓、期限，最后判断是否需要人工。"""
    settings = get_settings()
    now = _now(now)
    reasons: list[str] = []
    amount = float(order.amount)  # 将数据库 Numeric 转为便于比较和 JSON 输出的浮点数。

    # 以下条件按顺序提前返回，第一个不符合项决定原因；例如清仓商品不会再按大额转人工。
    # 归属检查优先：不能因退款资格判断而越权处理其他客户的订单。
    if customer is not None and order.customer_id != customer.id:
        reasons.append(
            f"订单 {order.id} 不属于当前客户。"
        )
        return PolicyResult(False, False, reasons, amount)

    # 未送达的商品不进入退款流程。
    delivered = _as_aware(order.delivered_date)
    if order.status != "delivered" or delivered is None:
        reasons.append(
            f"订单 {order.id} 尚未送达，暂不符合退款条件。"
        )
        return PolicyResult(False, False, reasons, amount)

    # 每笔订单只能成功退款一次。
    if order.refunded:
        reasons.append(f"订单 {order.id} 已经退款，不能重复办理。")
        return PolicyResult(False, False, reasons, amount)

    # 清仓商品即使金额很大也应拒绝，不能通过转人工绕过该规则。
    if order.is_final_sale:
        reasons.append(
            f"“{product_name(order.product_name)}”属于清仓不退商品，不支持退款。"
        )
        return PolicyResult(False, False, reasons, amount)

    # 保持原有整天计算：第 30 天可退，第 31 天超期。
    age_days = (now - delivered).days
    if age_days > settings.return_window_days:
        reasons.append(
            f"订单 {order.id} 已送达 {age_days} 天，超过 {settings.return_window_days} 天退货期限。"
        )
        return PolicyResult(False, False, reasons, amount)

    # 仅“超过”上限才转人工；金额和阈值始终以美元计。
    if amount > settings.escalation_threshold:
        reasons.append(
            f"退款金额 {amount:.2f} 美元超过 {settings.escalation_threshold:.0f} 美元的自动批准上限，需转人工审核。"
        )
        return PolicyResult(True, True, reasons, amount)

    reasons.append(
        f"订单 {order.id} 在 {settings.return_window_days} 天退货期限内，不属于清仓商品，且金额未超过自动批准上限。"
    )
    return PolicyResult(True, False, reasons, amount)
