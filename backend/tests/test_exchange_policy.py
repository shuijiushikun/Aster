from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.policy.exchange import evaluate_exchange

SUBMITTED = datetime(2026, 10, 21, tzinfo=timezone.utc)


def order(**changes):
    return SimpleNamespace(
        **(
            {
                "customer_id": "alice",
                "status": "delivered",
                "refunded": False,
                "is_final_sale": False,
                "delivered_date": SUBMITTED - timedelta(days=30),
            }
            | changes
        )
    )


def evaluate(candidate=None, **changes):
    args = {
        "customer_id": "alice",
        "submitted_at": SUBMITTED,
        "window_days": 30,
        "reason_type": "defect",
        "description": "右耳无声",
        "requested_change": "更换同款耳机",
    } | changes
    return evaluate_exchange(candidate or order(), **args)


def test_day_30_can_request_review_without_auto_approval():
    result = evaluate()
    assert result.status == "PENDING_REVIEW"
    assert result.missing_fields == []


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"customer_id": "bob"}, "NOT_FOUND_OR_NOT_OWNED"),
        ({"status": "shipped"}, "ORDER_NOT_DELIVERED"),
        ({"delivered_date": None}, "ORDER_NOT_DELIVERED"),
        ({"refunded": True}, "ORDER_REFUNDED"),
        ({"is_final_sale": True}, "FINAL_SALE"),
        ({"delivered_date": SUBMITTED - timedelta(days=31)}, "EXCHANGE_WINDOW_EXPIRED"),
    ],
)
def test_ineligible_orders_are_rejected(changes, code):
    result = evaluate(order(**changes))
    assert result.status == "REJECTED"
    assert result.code == code


@pytest.mark.parametrize("missing", ["reason_type", "requested_change", "description"])
def test_missing_information_waits_before_review(missing):
    result = evaluate(**{missing: None if missing == "reason_type" else "  "})
    assert result.status == "WAITING_INFO"
    assert missing in result.missing_fields


def test_deadline_uses_submitted_at_not_current_time():
    candidate = order(delivered_date=SUBMITTED.replace(tzinfo=None) - timedelta(days=30))
    assert evaluate(candidate).status == "PENDING_REVIEW"
    assert (
        evaluate(candidate, submitted_at=SUBMITTED + timedelta(days=1)).code
        == "EXCHANGE_WINDOW_EXPIRED"
    )


def test_invalid_reason_is_not_treated_as_a_valid_review():
    assert evaluate(reason_type="repair").status == "REJECTED"
