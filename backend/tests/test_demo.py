"""Public demo mode: isolated sandboxes, scoped admin, cost fallback."""

import json

import pytest
from starlette.requests import Request

from app import redis as shared
from app.config import Settings, _async_db_url, get_settings
from app.demo import client_ip


@pytest.fixture
def demo(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_mode", "demo")
    monkeypatch.setattr(settings, "llm_provider", "fake")
    monkeypatch.setattr(settings, "fake_llm_latency_ms", 0)
    return settings


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def new_session(client) -> dict:
    res = await client.post("/api/demo/session")
    assert res.status_code == 200, res.text
    return res.json()


async def chat(client, token: str, message: str) -> list[dict]:
    res = await client.post("/api/chat", json={"message": message}, headers=bearer(token))
    assert res.status_code == 200, res.text
    return [
        json.loads(line[5:].strip())
        for line in res.text.splitlines()
        if line.startswith("data:")
    ]


async def test_demo_routes_404_outside_demo_mode(client):
    assert (await client.post("/api/demo/session")).status_code == 404


async def test_session_creates_isolated_sandbox(client, demo):
    a = await new_session(client)
    b = await new_session(client)
    assert a["customer"]["id"] != b["customer"]["id"]
    assert a["customer"]["id"].startswith("DM-")
    scenarios = {o["scenario"] for o in a["orders"]}
    assert scenarios == {
        "refundable",
        "final_sale",
        "high_value",
        "out_of_window",
        "already_refunded",
    }

    # The customer token reads only its own orders.
    res = await client.get("/api/me/orders", headers=bearer(a["token"]))
    assert res.status_code == 200
    assert {o["id"] for o in res.json()} == {o["id"] for o in a["orders"]}


async def test_session_creation_is_rate_limited_per_ip(client, demo, monkeypatch):
    monkeypatch.setattr(demo, "demo_session_rate_limit", "2/hour")
    for _ in range(2):
        await new_session(client)
    res = await client.post("/api/demo/session")
    assert res.status_code == 429
    assert int(res.headers["Retry-After"]) > 0


async def test_scoped_admin_sees_only_own_conversations(client, demo):
    a = await new_session(client)
    b = await new_session(client)
    a_refundable = next(o["id"] for o in a["orders"] if o["scenario"] == "refundable")
    events_a = await chat(client, a["token"], f"Please refund order {a_refundable}")
    await chat(client, b["token"], "What orders do I have?")
    conv_a = next(e["conversation_id"] for e in events_a if e["kind"] == "conversation")

    listed = (await client.get("/api/conversations", headers=bearer(a["admin_token"]))).json()
    assert [c["id"] for c in listed] == [conv_a]

    # B's scoped admin can neither read nor stream A's conversation.
    for path in (f"/api/conversations/{conv_a}", f"/api/conversations/{conv_a}/stream"):
        res = await client.get(path, headers=bearer(b["admin_token"]))
        assert res.status_code == 404

    detail = await client.get(f"/api/conversations/{conv_a}", headers=bearer(a["admin_token"]))
    assert detail.status_code == 200
    stats = (await client.get("/api/admin/stats", headers=bearer(a["admin_token"]))).json()
    assert stats["conversations"] == 1
    assert stats["decisions"] == {"approved": 1}


async def test_scoped_admin_token_cannot_chat(client, demo):
    a = await new_session(client)
    res = await client.post("/api/chat", json={"message": "hi"}, headers=bearer(a["admin_token"]))
    assert res.status_code == 403


async def test_spent_budget_falls_back_to_scripted_model(client, demo, monkeypatch):
    from app.agent import graph as graph_module

    monkeypatch.setattr(demo, "demo_global_daily_token_budget", 100)
    await shared.add_customer_tokens("someone-else", 1_000)

    # Were the real model used, this would blow up.
    def broken_model():
        raise AssertionError("real model must not be used once the budget is spent")

    monkeypatch.setattr(graph_module, "get_chat_model", broken_model)
    a = await new_session(client)
    events = await chat(client, a["token"], "What orders do I have?")
    notices = [e for e in events if e.get("step_type") == "notice"]
    assert notices and notices[0]["payload"]["reason"] == "demo_budget"
    assert events[-1] == {"kind": "done"}
    # Scripted turns don't consume the budget.
    assert await shared.customer_tokens_today(a["customer"]["id"]) == 0


async def test_meta_reports_auth_mode(client, demo):
    body = (await client.get("/api/meta")).json()
    assert body["auth_mode"] == "demo"
    assert body["demo"]["data_ttl_hours"] == demo.demo_data_ttl_hours


async def test_meta_names_the_primary_model(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "llm_provider", "fake")
    assert (await client.get("/api/meta")).json()["model_name"] is None  # scripted
    monkeypatch.setattr(settings, "llm_provider", "omniroute")
    monkeypatch.setattr(settings, "omniroute_model", "cx/gpt-5.6-sol-medium")
    body = (await client.get("/api/meta")).json()
    # The proxy's routing prefix is an internal detail, so the UI gets the bare id.
    assert body["model"] == "omniroute"
    assert body["model_name"] == "gpt-5.6-sol-medium"


async def test_demo_purge_removes_old_sandboxes(client, demo, engine):
    from datetime import timedelta

    from sqlalchemy import func, select, update

    from app.db import session as db
    from app.db.models import Customer, Order, utcnow
    from app.maintenance.retention import purge_demo

    old = await new_session(client)
    fresh = await new_session(client)
    await chat(client, old["token"], "What orders do I have?")
    async with db.SessionLocal() as s:
        await s.execute(
            update(Customer)
            .where(Customer.id == old["customer"]["id"])
            .values(created_at=utcnow() - timedelta(hours=48))
        )
        await s.commit()

    assert (await purge_demo(24))[0] == 1
    async with db.SessionLocal() as s:
        ids = set((await s.scalars(select(Customer.id).where(Customer.id.like("DM-%")))).all())
        orphan_orders = await s.scalar(
            select(func.count(Order.id)).where(Order.customer_id == old["customer"]["id"])
        )
    assert ids == {fresh["customer"]["id"]}
    assert orphan_orders == 0


def _request(xff: str | None, peer: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", xff.encode())] if xff else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


def test_client_ip_uses_trusted_hops(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "trusted_proxy_hops", 0)
    assert client_ip(_request("1.1.1.1, 2.2.2.2")) == "10.0.0.1"
    monkeypatch.setattr(settings, "trusted_proxy_hops", 1)
    # A client-supplied "1.1.1.1" can't spoof the entry the proxy appended.
    assert client_ip(_request("1.1.1.1, 2.2.2.2")) == "2.2.2.2"
    assert client_ip(_request(None)) == "10.0.0.1"


def test_neon_style_urls_are_translated_for_asyncpg():
    url = _async_db_url(
        "postgresql://u:p@ep-x.neon.tech/db?sslmode=require&channel_binding=require"
    )
    assert url == "postgresql+asyncpg://u:p@ep-x.neon.tech/db?ssl=require"


def test_prod_accepts_demo_mode_only_with_strong_secret():
    base = dict(
        env="prod",
        auth_mode="demo",
        redis_url="redis://r:6379",
        database_url="postgresql://u:p@h/db",
    )
    with pytest.raises(ValueError, match="JWT_SECRET"):
        Settings(**base, jwt_secret="short")
    assert Settings(**base, jwt_secret="x" * 40).is_demo
