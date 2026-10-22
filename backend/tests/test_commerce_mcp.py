"""仅覆盖 T2 的真实 SDK/HTTP 合同、身份隔离和依赖失败。"""

import asyncio
import json
import time

import httpx
import jwt
import pytest
from sqlalchemy import func, select

from app.agent.tools import ToolContext, tool_config
from app.auth import Principal, mint_token
from app.commerce_mcp.server import create_app
from app.config import Settings
from app.db import seed
from app.db import session as db
from app.db.models import AfterSalesTicket, ExchangeAcceptance, Refund
from app.mcp.auth import mint_service_token
from app.mcp.client import CommerceClient
from app.mcp.tools import build_commerce_tools

ALICE = Principal("CUST-EX-001", "customer")
BOB = Principal("CUST-EX-002", "customer")


@pytest.fixture
def mcp_settings():
    return Settings(
        _env_file=None,
        commerce_mcp_service_secret="test-mcp-secret-which-is-at-least-32-chars",
        commerce_mcp_url="http://127.0.0.1:8001/mcp",
        commerce_mcp_timeout_s=2,
        commerce_mcp_max_retries=0,
    )


@pytest.fixture
async def mcp_client(engine, mcp_settings):
    await seed.seed_exchange_samples()
    app = create_app(mcp_settings)

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

    # pytest-asyncio 的 yield fixture 在不同 task 中收尾；SDK 的 AnyIO cancel scope
    # 必须在进入它的同一 task 退出，所以让应用 lifespan 拥有独立、完整的 task。
    started, stopped = asyncio.Event(), asyncio.Event()

    async def lifespan():
        async with app.router.lifespan_context(app):
            started.set()
            await stopped.wait()

    task = asyncio.create_task(lifespan())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        yield CommerceClient(mcp_settings, http_client_factory=factory), app, factory
    finally:
        stopped.set()
        await task


async def test_four_read_tools_and_customer_isolation(mcp_client):
    client, _, _ = mcp_client
    tools = await client.list_tools(ALICE)
    assert {t.name for t in tools} == {
        "list_my_orders",
        "get_order",
        "get_product",
        "get_exchange_policy_facts",
    }
    for tool in tools:
        assert tool.annotations.readOnlyHint is True
        assert tool.outputSchema
        assert not (
            {"customer_id", "sub", "token", "ctx", "config"} & tool.inputSchema["properties"].keys()
        )
    orders = await client.call(ALICE, "list_my_orders", {"limit": 1})
    assert orders.ok and len(orders.data.orders) == 1
    assert orders.data.orders[0].order_id in {"ORD-EX-1001", "ORD-EX-1002"}
    mine = await client.call(ALICE, "get_order", {"order_id": "ORD-EX-1001"})
    assert mine.ok and mine.data.model_code == "HP-01"
    other = await client.call(
        ALICE, "get_order", {"order_id": "ORD-EX-2001", "customer_id": BOB.sub}
    )
    missing = await client.call(ALICE, "get_order", {"order_id": "missing"})
    assert other.error == missing.error and other.data is None
    assert (await client.call(BOB, "get_order", {"order_id": "ORD-EX-2001"})).ok
    product = await client.call(ALICE, "get_product", {"model_code": mine.data.model_code})
    assert product.ok and product.data.product_id == mine.data.product_id
    facts = await client.call(ALICE, "get_exchange_policy_facts", {"order_id": mine.data.order_id})
    assert facts.ok and not facts.data.already_refunded and not facts.data.exchange_accepted
    assert not {"approved", "eligible", "decision"} & facts.data.model_dump().keys()
    legacy = await client.call(
        Principal("CUST-001", "customer"), "get_order", {"order_id": "ORD-1001"}
    )
    assert legacy.ok and legacy.data.model_code is None


@pytest.mark.parametrize(
    "case", ["missing", "frontend", "expired", "wrong_audience", "wrong_signature"]
)
async def test_server_rejects_untrusted_identity(mcp_client, mcp_settings, case):
    _, app, _ = mcp_client
    now = int(time.time())
    claims = {
        "sub": ALICE.sub,
        "iss": "support-backend",
        "aud": "commerce-mcp",
        "iat": now - 120,
        "exp": now + 60,
    }
    secret = mcp_settings.commerce_mcp_service_secret
    if case == "expired":
        claims["exp"] = now - 1
    if case == "wrong_audience":
        claims["aud"] = "some-other-service"
    if case == "wrong_signature":
        secret = "wrong-secret-which-is-at-least-32-chars"
    token = (
        mint_token(ALICE.sub)
        if case == "frontend"
        else jwt.encode(claims, secret, algorithm="HS256")
    )
    headers = {} if case == "missing" else {"Authorization": "Bearer " + token}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8001"
    ) as http:
        response = await http.post("/mcp", headers=headers, json={})
    assert response.status_code == 401
    assert token not in response.text


async def test_invalid_arguments_and_writes_are_not_exposed(mcp_client):
    client, _, _ = mcp_client
    result = await client.call(ALICE, "list_my_orders", {"limit": 21})
    assert not result.ok and result.error.code == "INVALID_ARGUMENT"
    result = await client.call(ALICE, "issue_refund", {"order_id": "ORD-EX-1001"})
    assert not result.ok and result.error.code == "INVALID_ARGUMENT"
    async with db.SessionLocal() as session:
        for model in (Refund, AfterSalesTicket, ExchangeAcceptance):
            assert await session.scalar(select(func.count()).select_from(model)) == 0


async def test_agent_adapter_uses_injected_identity_and_structured_results(mcp_client):
    client, _, _ = mcp_client
    tools = {tool.name: tool for tool in build_commerce_tools(client)}
    config = tool_config(ToolContext("conversation", ALICE.sub))
    result = json.loads(
        await tools["get_order"].ainvoke({"order_id": "ORD-EX-1001"}, config=config)
    )
    assert result["ok"] and result["data"]["model_code"] == "HP-01"
    result = json.loads(
        await tools["get_order"].ainvoke({"order_id": "ORD-EX-2001"}, config=config)
    )
    assert not result["ok"] and result["error"]["code"] == "NOT_FOUND"
    for tool in tools.values():
        assert "customer_id" not in tool.tool_call_schema.model_json_schema()["properties"]
    result = json.loads(
        await tools["list_my_orders"].ainvoke({}, config=tool_config(ToolContext("anonymous")))
    )
    assert result["error"]["code"] == "IDENTITY_NOT_VERIFIED"


@pytest.mark.parametrize(
    "error_type,code", [(httpx.ReadTimeout, "MCP_TIMEOUT"), (httpx.ConnectError, "MCP_UNAVAILABLE")]
)
async def test_failure_is_bounded_per_turn_and_never_falls_back_to_writes(
    engine, mcp_settings, error_type, code
):
    attempts = []

    async def fail(request):
        attempts.append(request)
        raise error_type("simulated dependency failure", request=request)

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(fail), **kwargs)

    mcp_settings.commerce_mcp_max_retries = 1
    tools = {
        tool.name: tool
        for tool in build_commerce_tools(CommerceClient(mcp_settings, http_client_factory=factory))
    }
    config = tool_config(ToolContext("failed-turn", ALICE.sub))
    result = json.loads(
        await tools["get_order"].ainvoke({"order_id": "ORD-EX-1001"}, config=config)
    )
    assert result["error"]["code"] == code and len(attempts) == 2
    await tools["list_my_orders"].ainvoke({}, config=config)
    assert len(attempts) == 2
    async with db.SessionLocal() as session:
        assert await session.scalar(select(func.count()).select_from(Refund)) == 0


def test_only_backend_can_issue_customer_context(mcp_settings):
    with pytest.raises(ValueError):
        mint_service_token(Principal("reviewer", "admin"), mcp_settings)
