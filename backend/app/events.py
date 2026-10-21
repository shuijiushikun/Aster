"""通过 Redis 发布/订阅机制，在不同 Pod 之间传递实时执行事件。

聊天接口运行智能体，将每个执行步骤保存到数据库后，通过此模块发布事件。
管理面板按会话订阅，并通过 SSE 实时接收这些事件。由于通信频道位于 Redis，
管理端连接的 Pod 可以与处理本轮对话的 Pod 不同。
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from app.redis import get_redis


def _channel(conversation_id: str) -> str:
    return f"conv:{conversation_id}"


class Broadcaster:
    async def publish(self, conversation_id: str, event: dict) -> None:
        await get_redis().publish(_channel(conversation_id), json.dumps(event))

    async def subscribe(self, conversation_id: str) -> AsyncIterator[dict]:
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(_channel(conversation_id))
        try:
            while True:
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=15.0
                )
                if msg is None:
                    continue
                if msg.get("type") == "message":
                    yield json.loads(msg["data"])
        finally:
            await pubsub.unsubscribe(_channel(conversation_id))
            await pubsub.aclose()


broadcaster = Broadcaster()
