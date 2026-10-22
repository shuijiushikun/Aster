"""LLM output is a proposal, never an authoritative business fact."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class IntentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    intent: Literal["refund", "exchange", "knowledge", "order", "ticket_followup", "other"]
    order_id: str | None
    needs_order: bool
    needs_knowledge: bool
    related_ticket_id: str | None


class ExchangeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    order_id: str = Field(min_length=1, max_length=128)
    reason_type: Literal["size", "defect"] | None
    description: str = Field(min_length=1, max_length=2000)
    requested_change: str | None = Field(max_length=500)
    evidence_document_ids: list[str] = Field(max_length=4)
    missing_information: list[str] = Field(max_length=8)
    recommended_action: Literal["request_info", "request_review", "reject"]
