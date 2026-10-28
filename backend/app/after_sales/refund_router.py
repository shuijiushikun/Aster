"""Customer confirmation API; deliberately absent from MCP/model tools."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict

from app import redis as shared
from app.after_sales.refund_confirmation import ConfirmationError, RefundConfirmationService
from app.auth import Principal, require_customer
from app.config import get_settings
from app.db.turns import StaleTurn
from app.mcp.schemas import RefundConfirmationView, RefundDecision

router = APIRouter(prefix="/api/me/conversations", tags=["refund-confirmation"])


class ConfirmationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["confirm", "cancel"]


class ConfirmationOutcome(BaseModel):
    id: str
    status: str
    result: RefundDecision | None = None


@router.get("/{cid}/refund-confirmation", response_model=RefundConfirmationView | None)
async def pending_confirmation(cid: str, principal: Principal = Depends(require_customer)):
    try:
        return await RefundConfirmationService().pending(principal.sub, cid)
    except ConfirmationError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from None


@router.post("/{cid}/refund-confirmations/{confirmation_id}", response_model=ConfirmationOutcome)
async def decide_confirmation(cid: str, confirmation_id: str, body: ConfirmationInput,
                              principal: Principal = Depends(require_customer)):
    service = RefundConfirmationService()
    # Check scope before touching the shared conversation lock.
    try:
        await service.pending(principal.sub, cid)
    except ConfirmationError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from None
    lock = await shared.acquire_turn_lock(cid, get_settings().turn_timeout_s + 30)
    if lock is None:
        raise HTTPException(status_code=409, detail="当前对话仍在处理中，请稍后确认。")
    try:
        return await service.decide(principal.sub, cid, confirmation_id, body.decision, lock_token=lock)
    except StaleTurn as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    except ConfirmationError as error:
        raise HTTPException(status_code=error.status_code, detail=error.message) from None
    finally:
        await shared.release_turn_lock(cid, lock)
