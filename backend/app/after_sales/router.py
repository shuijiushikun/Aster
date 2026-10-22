from fastapi import APIRouter, Depends, HTTPException, Query

from app.after_sales.schemas import AdminTicketDetail, TicketDetail, TicketStatus, TicketSummary
from app.after_sales.service import TicketError, TicketService
from app.auth import Principal, require_admin, require_customer

router = APIRouter(prefix="/api", tags=["after-sales"])


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
