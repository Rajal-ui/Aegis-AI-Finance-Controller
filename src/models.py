"""Pydantic V2 schemas for the three reconciliation sources."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, PositiveInt


class InternalOrder(BaseModel):
    transaction_id: str = Field(..., pattern=r"^ORD-\d{4}$")
    order_date: date
    customer_id: str
    amount: Decimal = Field(..., ge=0)
    currency: str = "USD"
    order_reference: str


class PaymentGatewaySettlement(BaseModel):
    settlement_id: str = Field(..., pattern=r"^SET-")
    settled_at: datetime
    merchant_reference: str
    gross_amount: Decimal = Field(..., ge=0)
    currency: str = "USD"
    status: Literal["settled"] = "settled"


class BankStatementRow(BaseModel):
    bank_transaction_id: str = Field(..., pattern=r"^BANK-")
    posted_at: datetime
    reference: str
    description: str
    amount: Decimal = Field(..., ge=0)
    currency: str = "USD"
    transaction_type: Literal["CREDIT", "DEBIT"] = "CREDIT"


class MatchedRecord(BaseModel):
    transaction_id: str
    gateway_settlement_id: str | None
    bank_transaction_id: str | None
    status: Literal["matched"]
    method: Literal["deterministic", "llm"]
    confidence: float = Field(..., ge=0, le=1)
    amount: Decimal


class UnmatchedException(BaseModel):
    transaction_id: str
    type: Literal["unmatched_exception"] = "unmatched_exception"
    reason: str
    gateway_candidates: list[str] = []
    bank_candidates: list[str] = []
    llm_confidence: float | None = None


class ReconciliationReport(BaseModel):
    report_version: str = "1.0"
    summary: dict
    matches: list[MatchedRecord] = []
    unmatched_exceptions: list[UnmatchedException] = []
