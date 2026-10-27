from langchain_core.messages import AIMessage, HumanMessage

from app.agent import context
from app.agent.chat_graph import scope_error
from app.db import session as db
from app.db.models import Conversation, Message
from app.db.seed import seed_demo_samples


async def test_short_order_reference_is_resolved_only_within_owned_orders(engine):
    from app.after_sales.references import resolve_order_reference

    await seed_demo_samples()
    async with db.SessionLocal() as session:
        assert await resolve_order_reference(session, "CUST-DEMO-001", "1005的续航") == "ORD-DEMO-1005"
        assert await resolve_order_reference(session, "CUST-DEMO-002", "1005的续航") is None
        assert await resolve_order_reference(session, "CUST-DEMO-001", "1001和1005的耳机") is None
        assert await resolve_order_reference(session, "CUST-DEMO-001", "ORD-DEMO-1001 和 ORD-DEMO-1005") is None
        assert await resolve_order_reference(session, "CUST-DEMO-001", "129.99美元") is None
        assert await resolve_order_reference(session, "CUST-DEMO-001", "1005美元") is None
        assert await resolve_order_reference(session, "CUST-DEMO-001", "1005/10/23") is None


async def test_short_followup_uses_previous_owned_reference_but_current_ambiguity_does_not(engine):
    from app.after_sales.references import resolve_order_reference

    await seed_demo_samples()
    async with db.SessionLocal() as session:
        assert await resolve_order_reference(session, "CUST-DEMO-001", "那能退款吗", previous_text="1005的订单") == "ORD-DEMO-1005"
        assert await resolve_order_reference(session, "CUST-DEMO-001", "1001和1005", previous_text="1005的订单") is None
        assert await resolve_order_reference(session, "CUST-DEMO-002", "那能退款吗", previous_text="1005的订单") is None
    async with db.SessionLocal() as session, session.begin():
        session.add(Conversation(id="suffix-followup", customer_id="CUST-DEMO-001"))
        await session.flush()
        session.add(Message(conversation_id="suffix-followup", role="user", content="1005的订单"))
        await session.flush()
        session.add(Message(conversation_id="suffix-followup", role="assistant", content="已查到。"))
        await session.flush()
        session.add(Message(conversation_id="suffix-followup", role="user", content="那这笔能退款吗"))
    from app.agent.memory import load_context

    messages = await load_context("CUST-DEMO-001", "suffix-followup")
    assert context.grounded_id(messages, "ORD-DEMO-1005", "ORD") == "ORD-DEMO-1005"
    assert context.grounded_id(messages, "ORD-DEMO-1001", "ORD") is None


def test_verified_reference_can_ground_tool_without_trusting_model_invented_order():
    messages = context.build_context([HumanMessage("1005的续航")], "职责", facts={
        "resolved_order_id": "ORD-DEMO-1005",
        "catalog_products": [{"model_code": "HP-01", "name": "HP-01 无线耳机"}],
    })
    assert context.grounded_id(messages, "ORD-DEMO-1005", "ORD") == "ORD-DEMO-1005"
    assert scope_error(messages, "get_order", {"order_id": "ORD-DEMO-1001"})
    assert scope_error(messages, "search_knowledge", {"model_code": "HP-01"})
    generic = context.build_context([HumanMessage("随便看看耳机相关知识")], "职责", facts={
        "catalog_products": [{"model_code": "HP-01", "name": "HP-01 无线耳机"}],
    })
    assert scope_error(generic, "search_knowledge", {"model_code": "HP-01"}) is None
    assert scope_error(generic, "search_knowledge", {"model_code": "HP-01", "overview": True}) is None
    application = context.build_context([HumanMessage("ORD-DEMO-1005 我要换货")], "职责")
    assert scope_error(application, "search_knowledge", {"order_id": "ORD-DEMO-1005", "overview": True})
    assert scope_error(generic, "search_knowledge", {"model_code": "FAKE-01"})
    explicit = context.build_context([HumanMessage("FAKE-01 的耳机功能")], "职责", facts={
        "catalog_products": [{"model_code": "HP-01", "name": "HP-01 无线耳机"}],
    })
    assert scope_error(explicit, "search_knowledge", {"model_code": "HP-01"})
    assert context.grounded_id([AIMessage("ORD-DEMO-1005"), HumanMessage("办理退款")], "ORD-DEMO-1005", "ORD") is None
