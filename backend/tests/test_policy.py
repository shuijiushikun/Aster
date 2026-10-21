from datetime import datetime, timedelta, timezone

from app.db.models import Customer, Order
from app.policy.engine import evaluate

NOW = datetime(2026, 5, 27, tzinfo=timezone.utc)


def make_order(**overrides) -> Order:
    defaults = dict(
        id="ORD-TEST",
        customer_id="CUST-001",
        product_name="Test Widget",
        category="general",
        amount=100.0,
        status="delivered",
        order_date=NOW - timedelta(days=10),
        delivered_date=NOW - timedelta(days=5),
        is_final_sale=False,
        refunded=False,
    )
    defaults.update(overrides)
    return Order(**defaults)


def test_standard_in_window_is_approved():
    result = evaluate(make_order(), now=NOW)
    assert result.eligible
    assert not result.requires_escalation
    assert result.decision == "approved"
    assert result.refund_amount == 100.0


def test_final_sale_is_denied():
    result = evaluate(make_order(is_final_sale=True), now=NOW)
    assert not result.eligible
    assert result.decision == "denied"
    assert any("清仓" in r for r in result.reasons)


def test_over_threshold_is_escalated():
    result = evaluate(make_order(amount=750.0), now=NOW)
    assert result.eligible
    assert result.requires_escalation
    assert result.decision == "escalated"


def test_exactly_threshold_is_approved_not_escalated():
    # Policy says refunds *over* $500 escalate; exactly $500 is fine.
    result = evaluate(make_order(amount=500.0), now=NOW)
    assert result.eligible
    assert not result.requires_escalation
    assert result.decision == "approved"


def test_outside_window_is_denied():
    result = evaluate(make_order(delivered_date=NOW - timedelta(days=45)), now=NOW)
    assert not result.eligible
    assert result.decision == "denied"
    assert any("退货期限" in r for r in result.reasons)


def test_window_boundary_30_days_in_31_days_out():
    inside = evaluate(make_order(delivered_date=NOW - timedelta(days=30)), now=NOW)
    assert inside.eligible
    outside = evaluate(make_order(delivered_date=NOW - timedelta(days=31)), now=NOW)
    assert not outside.eligible


def test_already_refunded_is_denied():
    result = evaluate(make_order(refunded=True), now=NOW)
    assert not result.eligible
    assert any("已经退款" in r for r in result.reasons)


def test_not_delivered_is_denied():
    result = evaluate(
        make_order(status="shipped", delivered_date=None), now=NOW
    )
    assert not result.eligible
    assert any("尚未送达" in r for r in result.reasons)


def test_ownership_mismatch_is_refused():
    other = Customer(id="CUST-999", name="Mallory", email="m@example.com")
    result = evaluate(make_order(customer_id="CUST-001"), customer=other, now=NOW)
    assert not result.eligible
    assert any("不属于" in r for r in result.reasons)


def test_final_sale_takes_priority_over_escalation():
    # A final-sale item over $500 is still simply denied, never escalated.
    result = evaluate(make_order(is_final_sale=True, amount=900.0), now=NOW)
    assert not result.eligible
    assert not result.requires_escalation
    assert result.decision == "denied"
