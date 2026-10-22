"""独立只读 MCP 冒烟：先启动商城服务，再运行 python -m scripts.commerce_mcp_smoke。"""

import argparse
import asyncio
import json

from app.auth import Principal
from app.mcp.client import CommerceClient, CommerceDependencyError


async def smoke(args) -> int:
    client = CommerceClient()
    principal = Principal(args.customer_id, "customer")
    cases = []
    try:
        tools = await client.list_tools(principal)
        assert {tool.name for tool in tools} == {
            "list_my_orders",
            "get_order",
            "get_product",
            "get_exchange_policy_facts",
        }
        assert all(tool.annotations.readOnlyHint for tool in tools)
        result = await client.call(principal, "list_my_orders", {"limit": 2})
        assert result.ok
        cases.append("list_my_orders")
        order = await client.call(principal, "get_order", {"order_id": args.order_id})
        assert order.ok and order.data.model_code
        cases.append("get_order")
        result = await client.call(principal, "get_product", {"model_code": order.data.model_code})
        assert result.ok
        cases.append("get_product")
        result = await client.call(
            principal, "get_exchange_policy_facts", {"order_id": args.order_id}
        )
        assert result.ok
        cases.append("get_exchange_policy_facts")
        other = await client.call(principal, "get_order", {"order_id": args.other_order_id})
        missing = await client.call(principal, "get_order", {"order_id": "MISSING-SMOKE-ORDER"})
        assert not other.ok and not missing.ok and other.error == missing.error
        assert other.error.code == "NOT_FOUND"
        cases.append("ownership_isolation")
    except (AssertionError, CommerceDependencyError) as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "cases": cases,
                    "error": error.code
                    if isinstance(error, CommerceDependencyError)
                    else "unexpected_result",
                }
            )
        )
        return 1
    print(json.dumps({"status": "passed", "tool_count": len(tools), "cases": cases}))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer-id", default="CUST-DEMO-001")
    parser.add_argument("--order-id", default="ORD-DEMO-1005")
    parser.add_argument("--other-order-id", default="ORD-DEMO-2001")
    return asyncio.run(smoke(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
