"""公开演示沙箱（`AUTH_MODE=demo`）。

访客无需账号即可体验在线智能体：`POST /api/demo/session` 创建一个临时客户，
为其生成覆盖各个退款规则分支的订单，并返回两个短期令牌：

- 用于聊天的**客户令牌**。身份仍然只取自令牌，使用与生产环境相同的认证流程。
- 用于管理面板的**受限管理员令牌**（`scope` 为该客户 ID），
  使访客只能查看自己的智能体执行记录。

滥用防护：按客户端 IP 限制沙箱创建频率，也可启用 Cloudflare Turnstile 验证；
聊天仍受每位客户的限流、对话轮数和并发上限约束；分别限制每位访客及整个演示的
模型用量预算，耗尽后切换到脚本模型（见 `agent/runner.py`）；数据保留任务在
`DEMO_DATA_TTL_HOURS` 小时后清理沙箱。
"""

from __future__ import annotations

import logging
import random
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError

from app import redis as shared
from app.auth import ROLE_ADMIN, ROLE_CUSTOMER, mint_token
from app.config import get_settings
from app.db import session as db
from app.db.fixtures import scenario_for, scenario_orders
from app.db.models import Customer
from app.localization import product_name
from app.observability import REJECTIONS, log_event
from app.schemas import OrderOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/demo", tags=["demo"])

DEMO_PREFIX = "DM-"
_TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


class DemoSessionRequest(BaseModel):
    turnstile_token: str | None = None


class DemoCustomer(BaseModel):
    id: str
    name: str
    email: str


class DemoSession(BaseModel):
    customer: DemoCustomer
    orders: list[OrderOut]
    token: str
    admin_token: str
    expires_in: int
    data_ttl_hours: int


def client_ip(request: Request) -> str:
    """根据 `TRUSTED_PROXY_HOPS` 和 X-Forwarded-For 获取调用方 IP。

    每个可信代理都会追加它看到的地址，因此实际客户端地址是从右侧数第 N 项；
    更靠左的内容可能由客户端自行设置。
    """
    hops = get_settings().trusted_proxy_hops
    forwarded = request.headers.get("x-forwarded-for", "")
    if hops > 0 and forwarded:
        chain = [p.strip() for p in forwarded.split(",") if p.strip()]
        if chain:
            return chain[max(0, len(chain) - hops)]
    return request.client.host if request.client else "unknown"


def new_demo_customer_id() -> str:
    # 仅使用数字，使 ID 与智能体熟悉的订单编号形式一致。
    return f"{DEMO_PREFIX}{secrets.randbelow(10**10):010d}"


async def _verify_turnstile(token: str | None, ip: str) -> bool:
    secret = get_settings().turnstile_secret
    if not secret:
        return True
    if not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=5) as http:
            res = await http.post(
                _TURNSTILE_URL, data={"secret": secret, "response": token, "remoteip": ip}
            )
        return bool(res.json().get("success"))
    except (httpx.HTTPError, ValueError):
        log_event(logger, "turnstile verification unavailable", level=logging.WARNING)
        return False


async def _create_sandbox() -> tuple[Customer, list]:
    for _ in range(5):
        cid = new_demo_customer_id()
        customer = Customer(
            id=cid, name="演示客户", email=f"{cid.lower()}@demo.acme.test"
        )
        orders = scenario_orders(cid, random.Random())
        try:
            async with db.SessionLocal() as session:
                session.add(customer)
                session.add_all(orders)
                await session.commit()
            return customer, orders
        except IntegrityError:  # ID 碰撞概率极低，发生时重试即可。
            continue
    raise HTTPException(status_code=503, detail="暂时无法创建演示环境，请稍后重试。")


def order_out(order) -> OrderOut:
    return OrderOut(
        id=order.id,
        product_name=product_name(order.product_name),
        amount=float(order.amount),
        status=order.status,
        order_date=order.order_date,
        delivered_date=order.delivered_date,
        is_final_sale=order.is_final_sale,
        refunded=order.refunded,
        scenario=scenario_for(order.id),
    )


@router.post("/session", response_model=DemoSession)
async def create_session(request: Request, body: DemoSessionRequest | None = None):
    settings = get_settings()
    if not settings.is_demo:
        raise HTTPException(status_code=404, detail="未找到请求的数据。")

    ip = client_ip(request)
    allowed, reset_in = await shared.hit_rate_limit(
        f"demo-session:ip:{ip}", settings.demo_session_rate_limit
    )
    if not allowed:
        REJECTIONS.labels("demo_rate_limited").inc()
        raise HTTPException(
            status_code=429,
            detail="当前网络创建演示环境过于频繁，请稍后重试。",
            headers={"Retry-After": str(reset_in)},
        )
    if not await _verify_turnstile(body.turnstile_token if body else None, ip):
        raise HTTPException(status_code=403, detail="验证未通过，请重试。")

    customer, orders = await _create_sandbox()
    ttl = settings.demo_token_ttl_s
    log_event(logger, "demo session created", customer_id=customer.id)
    return DemoSession(
        customer=DemoCustomer(id=customer.id, name=customer.name, email=customer.email),
        orders=[order_out(o) for o in orders],
        token=mint_token(customer.id, ROLE_CUSTOMER, ttl_s=ttl),
        admin_token=mint_token(f"demo-admin:{customer.id}", ROLE_ADMIN, ttl_s=ttl, scope=customer.id),
        expires_in=ttl,
        data_ttl_hours=settings.demo_data_ttl_hours,
    )
