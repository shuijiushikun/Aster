"""调用方先锁订单并鉴权，再读取或保存同事务的工具请求结果。"""

import hashlib
import json

from sqlalchemy import select

from app.db.models import AgentToolRequest


class RequestConflict(ValueError):
    pass


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


async def replay(session, customer_id, operation, request_id, payload):
    if request_id is None:
        return None
    row = await session.scalar(select(AgentToolRequest).where(
        AgentToolRequest.customer_id == customer_id,
        AgentToolRequest.operation == operation,
        AgentToolRequest.request_id == request_id,
    ))
    if row is None:
        return None
    if row.payload_hash != payload_hash(payload):
        raise RequestConflict("同一请求标识不能用于不同内容。")
    return row.response_json


def remember(session, customer_id, operation, request_id, payload, response):
    if request_id is not None:
        session.add(AgentToolRequest(
            customer_id=customer_id, operation=operation, request_id=request_id,
            payload_hash=payload_hash(payload), response_json=response,
        ))
