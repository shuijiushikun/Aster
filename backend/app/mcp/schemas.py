"""商城 MCP 的输入/输出合同；不包含可执行审批或写订单的指令。"""

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


RESULT_TYPES = {
    "list_my_orders": ReadResult[OrderPage],
    "get_order": ReadResult[OrderInfo],
    "get_product": ReadResult[ProductInfo],
    "get_exchange_policy_facts": ReadResult[ExchangeFacts],
}


def failure(code: str, message: str) -> ReadResult:
    return ReadResult(ok=False, error=CommerceError(code=code, message=message))
