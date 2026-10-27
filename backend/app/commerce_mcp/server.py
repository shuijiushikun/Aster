"""售后 MCP：python -m uvicorn app.commerce_mcp.server:create_app --factory --port 8001。"""

import logging
from contextlib import asynccontextmanager

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from starlette.applications import Starlette

from app.config import Settings, get_settings
from app.db import session as db
from app.db.models import ExchangeAcceptance, Order, Product
from app.localization import product_name
from app.mcp.auth import ServiceContextMiddleware, service_secret
from app.mcp.schemas import (
    ExchangeFacts,
    Identifier,
    OrderInfo,
    OrderLimit,
    OrderPage,
    ProductInfo,
    ReadResult,
)

log = logging.getLogger(__name__)
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


@asynccontextmanager
async def read_session():
    async with db.SessionLocal() as session, session.begin():
        if session.get_bind().dialect.name == "postgresql":
            await session.execute(text("SET TRANSACTION READ ONLY"))
        yield session


def customer_id(ctx: Context) -> str:
    # SDK 的 Context 来自当前 HTTP 请求，不属于工具输入 schema。
    return ctx.request_context.request.state.commerce_customer_id


def order_info(order: Order, model_code: str | None) -> OrderInfo:
    return OrderInfo(
        order_id=order.id,
        product_id=order.product_id,
        model_code=model_code,
        product_name=product_name(order.product_name)[:500],
        category=order.category[:128],
        amount=order.amount,
        status=order.status,
        order_date=order.order_date,
        delivered_date=order.delivered_date,
        is_final_sale=order.is_final_sale,
        already_refunded=order.refunded,
    )


def order_query(owner: str):
    # 归属过滤在 SQL 端执行，不信任客户提供的 order_id 或裸 customer_id。
    return (
        select(Order, Product.model_code)
        .outerjoin(Product, Product.id == Order.product_id)
        .where(Order.customer_id == owner)
    )


def unavailable(result_type, error):
    log.warning("commerce read failed: %s", type(error).__name__)
    return result_type(
        ok=False, error={"code": "MCP_UNAVAILABLE", "message": "商城查询暂时不可用，请稍后重试。"}
    )


def not_found(result_type):
    return result_type(ok=False, error={"code": "NOT_FOUND", "message": "未找到可访问的记录。"})


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or get_settings()
    secret = service_secret(settings)
    server = FastMCP(
        "commerce_mcp",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        instructions="提供本人订单、退款、知识检索和换货申请；审批和受理由独立业务工作流执行。",
    )

    @server.tool(annotations=READ_ONLY)
    async def list_my_orders(ctx: Context, limit: OrderLimit = 10) -> ReadResult[OrderPage]:
        """列出当前已登录客户最近的订单，limit 为 1～20；不接受客户身份参数。"""
        try:
            async with read_session() as session:
                rows = await session.execute(
                    order_query(customer_id(ctx))
                    .order_by(Order.order_date.desc(), Order.id.desc())
                    .limit(limit)
                )
                return ReadResult[OrderPage](
                    ok=True,
                    data=OrderPage(orders=[order_info(order, code) for order, code in rows]),
                )
        except SQLAlchemyError as error:
            return unavailable(ReadResult[OrderPage], error)

    @server.tool(annotations=READ_ONLY)
    async def get_order(order_id: Identifier, ctx: Context) -> ReadResult[OrderInfo]:
        """查询当前客户的一笔订单和可信商品型号；他人订单与不存在订单返回相同结果。"""
        try:
            async with read_session() as session:
                row = (
                    await session.execute(order_query(customer_id(ctx)).where(Order.id == order_id))
                ).first()
                return (
                    ReadResult[OrderInfo](ok=True, data=order_info(*row))
                    if row
                    else not_found(ReadResult[OrderInfo])
                )
        except SQLAlchemyError as error:
            return unavailable(ReadResult[OrderInfo], error)

    @server.tool(annotations=READ_ONLY)
    async def get_product(model_code: Identifier) -> ReadResult[ProductInfo]:
        """按已知商品型号读取公开商品信息；不查询客户、库存或售后写状态。"""
        try:
            async with read_session() as session:
                product = await session.scalar(
                    select(Product).where(
                        Product.model_code == model_code, Product.is_active.is_(True)
                    )
                )
                if product is None:
                    return not_found(ReadResult[ProductInfo])
                return ReadResult[ProductInfo](
                    ok=True,
                    data=ProductInfo(
                        product_id=product.id,
                        model_code=product.model_code,
                        name=product.name[:500],
                        category=product.category[:128],
                        is_active=product.is_active,
                    ),
                )
        except SQLAlchemyError as error:
            return unavailable(ReadResult[ProductInfo], error)

    @server.tool(annotations=READ_ONLY)
    async def get_exchange_policy_facts(
        order_id: Identifier, ctx: Context
    ) -> ReadResult[ExchangeFacts]:
        """读取本人订单的妥投、清仓、退款及换货受理事实，供后端计算政策；不返回最终资格或审批。"""
        try:
            async with read_session() as session:
                row = (
                    await session.execute(order_query(customer_id(ctx)).where(Order.id == order_id))
                ).first()
                if row is None:
                    return not_found(ReadResult[ExchangeFacts])
                order, code = row
                accepted = await session.scalar(
                    select(ExchangeAcceptance.id).where(ExchangeAcceptance.order_id == order_id)
                )
                return ReadResult[ExchangeFacts](
                    ok=True,
                    data=ExchangeFacts(
                        order_id=order.id,
                        model_code=code,
                        status=order.status,
                        delivered_date=order.delivered_date,
                        is_final_sale=order.is_final_sale,
                        already_refunded=order.refunded,
                        exchange_accepted=accepted is not None,
                    ),
                )
        except SQLAlchemyError as error:
            return unavailable(ReadResult[ExchangeFacts], error)

    from app.commerce_mcp.operations import register_business_tools

    register_business_tools(server, settings)
    app = server.streamable_http_app()
    app.add_middleware(ServiceContextMiddleware, secret=secret)
    transport_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette):
        try:
            async with transport_lifespan(app):
                yield
        finally:
            await db.dispose()

    app.router.lifespan_context = lifespan
    return app
