import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from app.after_sales.schemas import (
    AdminTicketDetail,
    DecisionInput,
    RequestAccepted,
    RequestInput,
    SupplementInput,
    TicketDetail,
    TicketStatus,
    TicketSummary,
)
from app.after_sales.service import TicketError, TicketService
from app.auth import Principal, require_admin, require_customer
from app.observability import log_event
from app.workflow.resume import coordinate

router = APIRouter(prefix="/api", tags=["after-sales"])
logger = logging.getLogger(__name__)


async def _result(operation):
    try:
        return await operation
    except TicketError as error:
        raise HTTPException(
            status_code=error.status_code, detail={"code": error.code, "message": error.message}
        ) from None


@router.get("/me/tickets", response_model=list[TicketSummary])
async def my_tickets(
    principal: Principal = Depends(require_customer),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
):
    return await _result(TicketService().list_tickets(principal, limit=limit, offset=offset))


@router.get("/me/tickets/{ticket_id}", response_model=TicketDetail)
async def my_ticket(ticket_id: str, principal: Principal = Depends(require_customer)):
    return await _result(TicketService().detail(principal, ticket_id))


@router.get("/admin/tickets", response_model=list[TicketSummary])
async def admin_tickets(
    principal: Principal = Depends(require_admin),
    status: TicketStatus | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
):
    return await _result(
        TicketService().list_tickets(principal, status=status, limit=limit, offset=offset)
    )


@router.get("/admin/tickets/{ticket_id}", response_model=AdminTicketDetail)
async def admin_ticket(ticket_id: str, principal: Principal = Depends(require_admin)):
    return await _result(TicketService().detail(principal, ticket_id))


async def _write(operation):
    snapshot = await _result(operation)
    # The durable 202 snapshot is deliberately independent of execution latency.
    # A failed wake-up never undoes a committed approval/material/cancellation.
    try:
        await coordinate(snapshot["ticket_id"])
    except Exception as exc:
        log_event(
            logger,
            "committed request awaits workflow coordination",
            level=logging.WARNING,
            ticket_id=snapshot["ticket_id"],
            error_type=type(exc).__name__,
        )
    return snapshot


@router.post("/me/tickets/{ticket_id}/supplement", status_code=202, response_model=RequestAccepted)
async def supplement(
    ticket_id: str, data: SupplementInput, principal: Principal = Depends(require_customer)
):
    return await _write(TicketService().supplement(principal, ticket_id, data))


@router.post("/me/tickets/{ticket_id}/cancel", status_code=202, response_model=RequestAccepted)
async def cancel(
    ticket_id: str, data: RequestInput, principal: Principal = Depends(require_customer)
):
    return await _write(TicketService().cancel(principal, ticket_id, data))


@router.post("/admin/tickets/{ticket_id}/decision", status_code=202, response_model=RequestAccepted)
async def decision(
    ticket_id: str, data: DecisionInput, principal: Principal = Depends(require_admin)
):
    return await _write(TicketService().decide(principal, ticket_id, data))


@router.post("/admin/tickets/{ticket_id}/resume", status_code=202, response_model=RequestAccepted)
async def resume(ticket_id: str, data: RequestInput, principal: Principal = Depends(require_admin)):
    return await _write(TicketService().request_resume(principal, ticket_id, data))
