"""Bounded, serialized reconciliation from committed facts to a graph cursor."""

import asyncio
import logging

from langgraph.types import Command

from app import redis as shared
from app.after_sales.service import TicketService
from app.config import get_settings
from app.events import broadcaster
from app.observability import log_event
from app.workflow.graph import TERMINAL_STATUSES
from app.workflow.runtime import get_runtime

logger = logging.getLogger(__name__)


async def drive(runtime, ticket_id: str):
    service = TicketService()
    fact = await service.workflow_fact(ticket_id)
    config = {"configurable": {"thread_id": fact["thread_id"]}, "recursion_limit": 12}
    state = await runtime.graph.aget_state(config)
    if state.values and state.values.get("ticket_id") != ticket_id:
        raise ValueError("checkpoint ticket mismatch")
    pending = [item for task in state.tasks for item in task.interrupts]
    if pending:
        responses = {}
        for item in pending:
            payload = item.value
            if (
                not isinstance(payload, dict)
                or payload.get("ticket_id") != ticket_id
                or payload.get("kind") not in ("information", "approval")
            ):
                raise ValueError("unexpected workflow interrupt")
            waiting = "WAITING_INFO" if payload["kind"] == "information" else "PENDING_REVIEW"
            changed = fact["status"] != waiting or fact["version"] > payload["version"]
            if payload["kind"] == "approval":
                changed = changed or fact["approval"] is not None
            if changed:
                responses[item.id] = {"source": "persisted_facts"}
        if not responses:
            return "interrupted"
        await runtime.graph.ainvoke(Command(resume=responses), config=config)
    elif state.next:
        # Includes nodes that failed before their next checkpoint was committed.
        await runtime.graph.ainvoke(None, config=config)
    elif fact["status"] not in TERMINAL_STATUSES:
        await runtime.graph.ainvoke({"ticket_id": ticket_id}, config=config)
    else:
        return "finished"
    state = await runtime.graph.aget_state(config)
    return "interrupted" if any(task.interrupts for task in state.tasks) else "finished"


async def coordinate(ticket_id: str) -> dict:
    """Only after the caller commits and authorizes a durable business request."""
    runtime = get_runtime()
    state, token, fact = "unavailable", None, None
    settings = get_settings()
    try:
        # Acquisition, checkpoint reads and notifications share the same bound.
        async with asyncio.timeout(settings.workflow_timeout_s):
            if runtime is not None:
                token = await shared.acquire_workflow_lock(
                    ticket_id, settings.workflow_timeout_s + 15
                )
                if token is None:
                    state = "busy"
                else:
                    try:
                        state = await drive(runtime, ticket_id)
                    except Exception as exc:
                        log_event(
                            logger,
                            "workflow coordination deferred",
                            level=logging.WARNING,
                            ticket_id=ticket_id,
                            error_type=type(exc).__name__,
                        )
                        state = "deferred"
            fact = await TicketService().workflow_fact(ticket_id)
            if fact["conversation_id"]:
                try:
                    await broadcaster.publish(
                        fact["conversation_id"],
                        {
                            "kind": "ticket_update",
                            "ticket_id": ticket_id,
                            "status": fact["status"],
                        },
                    )
                    await broadcaster.publish(
                        fact["conversation_id"],
                        {
                            "kind": "workflow_status",
                            "ticket_id": ticket_id,
                            "state": state,
                        },
                    )
                except Exception as exc:
                    log_event(
                        logger,
                        "workflow notification deferred",
                        level=logging.WARNING,
                        ticket_id=ticket_id,
                        error_type=type(exc).__name__,
                    )
    except Exception as exc:
        # No raw dependency exception/connection string enters ticket output.
        log_event(
            logger,
            "workflow coordination deferred",
            level=logging.WARNING,
            ticket_id=ticket_id,
            error_type=type(exc).__name__,
        )
        state = "deferred"
    finally:
        if token is not None:
            try:
                async with asyncio.timeout(5):
                    await shared.release_workflow_lock(ticket_id, token)
            except Exception as exc:
                log_event(
                    logger,
                    "workflow lock release deferred",
                    level=logging.WARNING,
                    ticket_id=ticket_id,
                    error_type=type(exc).__name__,
                )
    return {"state": state, "status": fact["status"] if fact else None}
