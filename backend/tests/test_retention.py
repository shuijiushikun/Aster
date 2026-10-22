"""Summary and ticket foreign keys remain compatible with existing retention."""

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.after_sales.schemas import DecisionInput, ExchangeInput
from app.after_sales.service import TicketService
from app.agent.runner import create_conversation
from app.auth import Principal
from app.db import session as db
from app.db.models import (
    AfterSalesTicket,
    Conversation,
    ConversationArchive,
    ConversationSummaryRecord,
    Customer,
    ExchangeAcceptance,
    Message,
    TicketApproval,
    TicketEvent,
    TicketRequest,
    utcnow,
)
from app.maintenance.retention import purge, purge_demo
from tests.test_demo import demo, new_session  # noqa: F401 -- fixture registration


async def summary(cid, owner):
    async with db.SessionLocal() as session:
        message = Message(conversation_id=cid, role="user", content="售后问题")
        session.add(message)
        await session.flush()
        session.add(ConversationSummaryRecord(conversation_id=cid, customer_id=owner,
                    summary_text="客户咨询换货。", covered_message_id=message.id))
        await session.commit()


async def test_normal_retention_detaches_origin_and_keeps_business_facts(engine):
    cid = await create_conversation("CUST-001")
    await summary(cid, "CUST-001")
    ticket = await TicketService().create_or_find_exchange(
        Principal("CUST-001", "customer"), "ORD-1001",
        ExchangeInput(description="耳机无声", reason_type="defect", requested_change="更换同款"),
        origin_conversation_id=cid,
    )
    async with db.SessionLocal() as session:
        (await session.get(Conversation, cid)).created_at = utcnow() - timedelta(days=91)
        await session.commit()
    assert await purge(90) == 1
    async with db.SessionLocal() as session:
        kept = await session.get(AfterSalesTicket, ticket.id)
        assert kept.status == "PENDING_REVIEW" and kept.origin_conversation_id is None
        assert await session.get(ConversationSummaryRecord, cid) is None


@pytest.mark.usefixtures("demo")
@pytest.mark.parametrize("purge_chat_first", [False, True])
async def test_demo_retention_archives_and_removes_summary_and_exchange_children(client, purge_chat_first):
    sandbox = await new_session(client)
    owner = sandbox["customer"]["id"]
    order_id = next(o["id"] for o in sandbox["orders"] if o["scenario"] == "refundable")
    cid = await create_conversation(owner)
    await summary(cid, owner)
    service = TicketService()
    ticket = await service.create_or_find_exchange(Principal(owner, "customer"), order_id,
        ExchangeInput(description="耳机无声", reason_type="defect", requested_change="更换同款"),
        origin_conversation_id=cid)
    await service.decide(Principal("retention-test", "admin"), ticket.id,
                         DecisionInput(request_id="approval", decision="approved"))
    receipt = await service.accept_exchange(ticket.id)
    async with db.SessionLocal() as session:
        (await session.get(Customer, owner)).created_at = utcnow() - timedelta(hours=48)
        if purge_chat_first:
            (await session.get(Conversation, cid)).created_at = utcnow() - timedelta(days=91)
        await session.commit()
    if purge_chat_first:
        assert await purge(90) == 1
    assert (await purge_demo(24))[:2] == (1, 1)
    async with db.SessionLocal() as session:
        record = await session.get(ConversationArchive, "demo-snapshot:" + owner if purge_chat_first else cid)
        assert record.transcript["tickets"][0]["reference_code"] == receipt.reference_code
        if purge_chat_first:
            assert record.transcript["snapshot_kind"] == "sandbox_without_conversation"
        else:
            assert record.transcript["summary"]["text"] == "客户咨询换货。"
        for model in (AfterSalesTicket, ConversationSummaryRecord, TicketApproval,
                      TicketEvent, TicketRequest, ExchangeAcceptance):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
