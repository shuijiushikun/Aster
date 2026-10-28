"""售后 MCP 的结构化合同；客户工具不包含管理员审批和受理入口。"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rag.schemas import RetrievalResult

Identifier = Annotated[str, Field(min_length=1, max_length=128)]
OrderLimit = Annotated[int, Field(ge=1, le=20)]


class CommerceError(BaseModel):
    code: Literal[
        "IDENTITY_NOT_VERIFIED",
        "NOT_FOUND",
        "INVALID_ARGUMENT",
        "MCP_TIMEOUT",
        "MCP_UNAVAILABLE",
        "INVALID_RESPONSE",
        "ACTION_NOT_REQUESTED",
        "REQUEST_CONFLICT",
        "EVIDENCE_REQUIRED",
        "BUSINESS_REJECTED",
        "OUTCOME_UNKNOWN",
    ]
    message: str


T = TypeVar("T")


class ReadResult(BaseModel, Generic[T]):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    data: T | None = None
    error: CommerceError | None = None

    @model_validator(mode="after")
    def consistent_result(self):
        if self.ok != (self.data is not None) or self.ok == (self.error is not None):
            raise ValueError("result must contain either data or an error")
        return self


class OrderInfo(BaseModel):
    order_id: Identifier
    product_id: Identifier | None
    model_code: Identifier | None
    product_name: str = Field(max_length=500)
    category: str = Field(max_length=128)
    amount: Decimal
    currency: Literal["USD"] = "USD"
    status: str
    order_date: datetime
    delivered_date: datetime | None
    is_final_sale: bool
    already_refunded: bool


class OrderPage(BaseModel):
    orders: list[OrderInfo] = Field(max_length=20)


class ProductInfo(BaseModel):
    product_id: Identifier
    model_code: Identifier
    name: str = Field(max_length=500)
    category: str = Field(max_length=128)
    is_active: bool


class ExchangeFacts(BaseModel):
    order_id: Identifier
    model_code: Identifier | None
    status: str
    delivered_date: datetime | None
    is_final_sale: bool
    already_refunded: bool
    exchange_accepted: bool


class CustomerProfile(BaseModel):
    customer_id: Identifier
    name: str
    email: str
    loyalty_tier: str


class RefundEligibility(BaseModel):
    order_id: Identifier
    eligible: bool
    requires_escalation: bool
    reasons: list[str]
    refund_amount: float
    decision: Literal["approved", "denied", "escalated"]


class RefundConfirmationView(BaseModel):
    id: Identifier
    conversation_id: Identifier
    order_id: Identifier
    product_name: str
    model_code: str | None
    amount: Decimal
    currency: Literal["USD"]
    operation: Literal["issue_refund", "escalate_to_human"]
    status: Literal["pending", "confirmed", "cancelled", "expired", "stale"]
    expires_at: datetime


class RefundDecision(BaseModel):
    order_id: Identifier
    decision: Literal["approved", "denied", "escalated", "awaiting_confirmation"]
    confirmation: RefundConfirmationView | None = None
    refund_recorded: bool = False
    amount: float = 0.0
    reasons: list[str] = Field(default_factory=list)
    already_escalated: bool = False
    message: str = ""


class KnowledgeSearch(RetrievalResult):
    evidence_token: str | None = Field(default=None, max_length=16000)


class TicketInfo(BaseModel):
    id: Identifier
    order_id: Identifier
    status: str
    reference_code: str | None = None


class TicketPage(BaseModel):
    tickets: list[TicketInfo] = Field(max_length=20)


RESULT_TYPES = {
    "list_my_orders": ReadResult[OrderPage],
    "get_order": ReadResult[OrderInfo],
    "get_product": ReadResult[ProductInfo],
    "get_exchange_policy_facts": ReadResult[ExchangeFacts],
    "get_my_profile": ReadResult[CustomerProfile],
    "check_refund_eligibility": ReadResult[RefundEligibility],
    "issue_refund": ReadResult[RefundDecision],
    "escalate_to_human": ReadResult[RefundDecision],
    "search_knowledge": ReadResult[KnowledgeSearch],
    "create_exchange_request": ReadResult[TicketInfo],
    "get_tickets": ReadResult[TicketPage],
}

WRITE_TOOLS = frozenset({"issue_refund", "escalate_to_human", "create_exchange_request"})


def failure(code: str, message: str) -> ReadResult:
    return ReadResult(ok=False, error=CommerceError(code=code, message=message))
