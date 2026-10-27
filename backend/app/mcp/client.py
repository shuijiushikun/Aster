"""官方 SDK 的 Streamable HTTP 客户端；依赖失败时绝不转为本地业务写入。"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from pydantic import ValidationError

from app.auth import Principal
from app.config import Settings, get_settings
from app.mcp.auth import mint_service_token
from app.mcp.schemas import RESULT_TYPES, WRITE_TOOLS, ReadResult, failure

log = logging.getLogger(__name__)


class CommerceDependencyError(Exception):
    def __init__(self, code: str):
        self.code = code


def _leaves(error):
    if isinstance(error, BaseExceptionGroup):
        return [leaf for nested in error.exceptions for leaf in _leaves(nested)]
    return [error]


def _error_code(error: Exception) -> str:
    leaves = _leaves(error)
    if any(
        isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403)
        for e in leaves
    ):
        return "IDENTITY_NOT_VERIFIED"
    if any(
        isinstance(e, (TimeoutError, httpx.TimeoutException))
        or (isinstance(e, McpError) and e.error.code == 408)
        for e in leaves
    ):
        return "MCP_TIMEOUT"
    return "MCP_UNAVAILABLE"


class CommerceClient:
    def __init__(self, settings: Settings | None = None, *, http_client_factory=httpx.AsyncClient):
        self.settings = settings or get_settings()
        self.http_client_factory = http_client_factory

    async def _request(self, principal: Principal, operation: Callable[[ClientSession], Awaitable],
                       *, turn_context: dict | None = None):
        if principal.role != "customer" or not principal.sub.strip():
            raise CommerceDependencyError("IDENTITY_NOT_VERIFIED")
        for attempt in range(self.settings.commerce_mcp_max_retries + 1):
            try:
                # 一次连接/初始化/调用/关闭整体有界；每次重试重新签发短时上下文。
                async with asyncio.timeout(self.settings.commerce_mcp_timeout_s):
                    token = mint_service_token(principal, self.settings, **(turn_context or {}))
                    async with (
                        self.http_client_factory(
                            headers={"Authorization": "Bearer " + token},
                            timeout=httpx.Timeout(self.settings.commerce_mcp_timeout_s),
                        ) as http_client,
                        streamable_http_client(
                            self.settings.commerce_mcp_url,
                            http_client=http_client,
                        ) as (read, write, _),
                    ):
                        async with ClientSession(
                            read,
                            write,
                            read_timeout_seconds=timedelta(
                                seconds=self.settings.commerce_mcp_timeout_s
                            ),
                        ) as session:
                            await session.initialize()
                            return await operation(session)
            except Exception as error:
                code = _error_code(error)
                if (
                    code == "IDENTITY_NOT_VERIFIED"
                    or attempt == self.settings.commerce_mcp_max_retries
                ):
                    log.warning("commerce MCP request failed: %s", type(error).__name__)
                    raise CommerceDependencyError(code) from None

    async def list_tools(self, principal: Principal):
        return (await self._request(principal, lambda session: session.list_tools())).tools

    async def call(self, principal: Principal, name: str, arguments: dict,
                   *, turn_context: dict | None = None) -> ReadResult:
        result_type = RESULT_TYPES.get(name)
        if result_type is None:
            return failure("INVALID_ARGUMENT", "未知的售后工具。")
        try:
            result = await self._request(
                principal, lambda session: session.call_tool(name, arguments), turn_context=turn_context
            )
        except CommerceDependencyError as error:
            if name in WRITE_TOOLS and error.code != "IDENTITY_NOT_VERIFIED":
                return failure("OUTCOME_UNKNOWN", "暂未取得办理结果，请查询订单或工单确认状态。重试同一请求不会重复办理。")
            messages = {
                "IDENTITY_NOT_VERIFIED": "商城查询身份无效，请重新登录。",
                "MCP_TIMEOUT": "商城查询超时，本轮无法确认订单事实。",
                "MCP_UNAVAILABLE": "商城查询暂时不可用，本轮无法确认订单事实。",
            }
            return failure(error.code, messages[error.code])
        if result.isError:
            if name in WRITE_TOOLS:
                return failure("OUTCOME_UNKNOWN", "服务端未返回有效办理结果，请查询实际状态后重试。")
            return failure("INVALID_ARGUMENT", "商城查询参数无效，请核对订单号、商品型号或数量。")
        try:
            return result_type.model_validate(result.structuredContent)
        except ValidationError:
            if name in WRITE_TOOLS:
                return failure("OUTCOME_UNKNOWN", "办理响应无效，请查询订单或工单确认实际结果。")
            return failure("INVALID_RESPONSE", "商城返回的数据格式无效，本轮不能据此执行操作。")
