"""Checkpoint stores the cursor; every node re-reads business authority."""

from typing import TypedDict

from langgraph.graph import END, StateGraph
from langgraph.types import interrupt
from sqlalchemy.exc import DBAPIError, OperationalError

from app.after_sales.service import TicketService

TERMINAL_STATUSES = frozenset({"COMPLETED", "REJECTED", "CANCELLED", "FAILED_TERMINAL"})


def retryable_execution_error(exc: Exception) -> bool:
    if isinstance(exc, (OperationalError, TimeoutError, ConnectionError, OSError)):
        return True
    if isinstance(exc, DBAPIError):
        # asyncpg disconnects and serialization/deadlock errors are not always
        # wrapped as OperationalError. A rolled-back transaction can be retried.
        code = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", "") or ""
        return (
            exc.connection_invalidated
            or code.startswith("08")
            or code
            in (
                "40001",
                "40P01",
                "55P03",
                "57P01",
                "57P02",
                "57P03",
            )
        )
    return False


class WorkflowState(TypedDict, total=False):
    ticket_id: str
    status: str


async def validate(state: WorkflowState):
    return await TicketService().validate_workflow(state["ticket_id"])


async def wait_information(state: WorkflowState):
    fact = await TicketService().workflow_fact(state["ticket_id"])
    if fact["status"] == "WAITING_INFO":
        interrupt(
            {
                "kind": "information",
                "ticket_id": fact["ticket_id"],
                "version": fact["version"],
                "missing_fields": fact["missing_fields"],
            }
        )
    # Resume payload is only a wake-up signal, never new materials or approval.
    fact = await TicketService().workflow_fact(state["ticket_id"])
    return {"status": fact["status"]}


async def wait_approval(state: WorkflowState):
    fact = await TicketService().workflow_fact(state["ticket_id"])
    if fact["status"] == "PENDING_REVIEW" and fact["approval"] is None:
        interrupt({"kind": "approval", "ticket_id": fact["ticket_id"], "version": fact["version"]})
    fact = await TicketService().workflow_fact(state["ticket_id"])
    return {"status": fact["status"]}


async def execute(state: WorkflowState):
    service = TicketService()
    fact = await service.workflow_fact(state["ticket_id"])
    if fact["status"] not in ("APPROVED", "FAILED_RETRYABLE"):
        return {"status": fact["status"]}
    try:
        await service.accept_exchange(state["ticket_id"])
    except Exception as exc:
        # The service's own transaction has already rolled back, or committed.
        # A new locked transaction checks that distinction before marking failure.
        retryable = retryable_execution_error(exc)
        return await service.mark_execution_failure(state["ticket_id"], retryable=retryable)
    return {"status": (await service.workflow_fact(state["ticket_id"]))["status"]}


def route(state):
    return {
        "WAITING_INFO": "wait_information",
        "PENDING_REVIEW": "wait_approval",
        "APPROVED": "execute",
        "FAILED_RETRYABLE": "execute",
    }.get(state["status"], END)


def compile_workflow(checkpointer):
    graph = StateGraph(WorkflowState)
    graph.add_node("validate", validate)
    graph.add_node("wait_information", wait_information)
    graph.add_node("wait_approval", wait_approval)
    graph.add_node("execute", execute)
    graph.set_entry_point("validate")
    graph.add_conditional_edges("validate", route)
    graph.add_edge("wait_information", "validate")
    graph.add_edge("wait_approval", "validate")
    graph.add_edge("execute", END)
    return graph.compile(checkpointer=checkpointer)
