from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

TicketStatus = Literal[
    "OPEN",
    "WAITING_INFO",
    "PENDING_REVIEW",
    "APPROVED",
    "FAILED_RETRYABLE",
    "COMPLETED",
    "REJECTED",
    "FAILED_TERMINAL",
    "CANCELLED",
]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class EvidenceRef(InputModel):
    document_id: str = Field(min_length=1, max_length=128)
    revision: str = Field(min_length=1, max_length=64)
    chunk_index: int = Field(ge=0)
    excerpt: str = Field(default="", max_length=300)


class ExchangeInput(InputModel):
    description: str = Field(min_length=1, max_length=2000)
    reason_type: Literal["size", "defect"] | None = None
    requested_change: str | None = Field(default=None, max_length=500)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list, max_length=5)


class RequestInput(InputModel):
    request_id: str = Field(min_length=1, max_length=128)


class SupplementInput(RequestInput):
    description: str = Field(min_length=1, max_length=2000)
    reason_type: Literal["size", "defect"] | None = None
    requested_change: str | None = Field(default=None, max_length=500)


class DecisionInput(RequestInput):
    decision: Literal["approved", "rejected"]
    comment: str = Field(default="", max_length=1000)


class RequestAccepted(BaseModel):
    ticket_id: str
    status: TicketStatus
    request_id: str


class TicketSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    order_id: str
    type: str
    status: TicketStatus
    description: str
    created_at: datetime
    updated_at: datetime
    last_error_code: str | None


class TicketEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    actor_type: str
    payload_json: dict
    created_at: datetime


class TicketDetail(TicketSummary):
    reason_type: str | None
    requested_change: str | None
    evidence_refs: list[EvidenceRef]
    reference_code: str | None
    events: list[TicketEventOut]


class ApprovalOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    decision: str
    admin_subject: str
    comment: str
    created_at: datetime


class TicketOrderOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    product_id: str | None
    product_name: str
    status: str
    refunded: bool
    is_final_sale: bool
    delivered_date: datetime | None


class AdminTicketDetail(TicketDetail):
    customer_id: str
    order: TicketOrderOut
    approval: ApprovalOut | None
    last_error_message: str | None
