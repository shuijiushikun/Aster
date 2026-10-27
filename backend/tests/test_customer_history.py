"""客户历史记录必须可恢复，并与管理员和其他客户隔离。"""

from app.db import session as db
from app.db.models import Conversation, Message, ReasoningEvent
from tests.conftest import admin_auth, auth


async def test_customer_can_reopen_only_own_conversations(client):
    async with db.SessionLocal() as session, session.begin():
        session.add_all([
            Conversation(id="history-own", customer_id="CUST-001"),
            Conversation(id="history-other", customer_id="CUST-002"),
            Conversation(id="history-empty", customer_id="CUST-001"),
        ])
        await session.flush()
        message = Message(conversation_id="history-own", role="user", content="1005的续航")
        session.add_all([message, Message(conversation_id="history-other", role="user", content="别人的问题")])
        await session.flush()
        session.add(ReasoningEvent(conversation_id="history-own", message_id=message.id,
                                   seq=1, step_type="tool_call", node="tools", payload={"tool": "get_order"}))
    response = await client.get("/api/me/conversations", headers=auth("CUST-001"))
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == ["history-own"]
    detail = await client.get("/api/me/conversations/history-own", headers=auth("CUST-001"))
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["content"] == "1005的续航"
    assert detail.json()["events"][0]["message_id"] == message.id
    for cid in ("history-other", "missing"):
        assert (await client.get(f"/api/me/conversations/{cid}", headers=auth("CUST-001"))).status_code == 404
    assert (await client.get("/api/me/conversations", headers=admin_auth())).status_code == 403
    assert (await client.get("/api/conversations", headers=auth("CUST-001"))).status_code == 403
    assert (await client.get("/api/me/conversations")).status_code == 401
