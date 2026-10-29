"""Core reconciliation engine.

Loads three JSONL sources, normalizes references, performs deterministic
exact matching on (normalized reference, amount-to-cent), routes unresolved
cases to an LLMResolver boundary, and emits a bounded exception report.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from pydantic import ValidationError

from .models import (
    BankStatementRow,
    InternalOrder,
    MatchedRecord,
    PaymentGatewaySettlement,
    ReconciliationReport,
    UnmatchedException,
)

RECORD_COUNT = 100
DEFAULT_MAX_EXCEPTIONS = 20


class LLMDecision:
    def __init__(
        self,
        accepted: bool,
        gateway_id: str | None,
        bank_id: str | None,
        confidence: float,
        reason: str,
    ) -> None:
        self.accepted = accepted
        self.gateway_id = gateway_id
        self.bank_id = bank_id
        self.confidence = confidence
        self.reason = reason


class LLMResolver(Protocol):
    def resolve(
        self,
        order: dict[str, Any],
        gateway_candidates: list[dict[str, Any]],
        bank_candidates: list[dict[str, Any]],
    ) -> LLMDecision:
        ...


class HeuristicLLMResolver:
    """Offline stand-in with the same contract expected from a real LLM."""

    def resolve(
        self,
        order: dict[str, Any],
        gateway_candidates: list[dict[str, Any]],
        bank_candidates: list[dict[str, Any]],
    ) -> LLMDecision:
        gateway = gateway_candidates[0] if len(gateway_candidates) == 1 else None
        bank = bank_candidates[0] if len(bank_candidates) == 1 else None
        order_amount = Decimal(str(order["amount"]))
        gateway_variance = (
            None
            if gateway is None
            else abs(Decimal(str(gateway["gross_amount"])) - order_amount)
        )
        bank_variance = (
            None if bank is None else abs(Decimal(str(bank["amount"])) - order_amount)
        )
        accepted = (
            gateway is not None
            and bank is not None
            and gateway_variance == 0
            and bank_variance == 0
        )
        if gateway is None or bank is None:
            reason = "A source row is missing or has no unique candidate."
        elif gateway_variance != 0:
            reason = f"Gateway amount variance is {gateway_variance:.2f}; hold for review."
        elif bank_variance != 0:
            reason = f"Bank amount variance is {bank_variance:.2f}; hold for review."
        else:
            reason = "Unique candidate resolved after deterministic matching was inconclusive."
        return LLMDecision(
            accepted,
            gateway["settlement_id"] if gateway else None,
            bank["bank_transaction_id"] if bank else None,
            0.86 if accepted else 0.62,
            reason,
        )


def normalize_reference(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def parse_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Invalid monetary value: {value!r}") from error


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def validate_sources(
    sources: dict[str, list[dict[str, Any]]],
) -> dict[str, list[Any]]:
    """Validate raw source dicts against Pydantic models, returning typed records."""
    orders: list[InternalOrder] = []
    gateway: list[PaymentGatewaySettlement] = []
    bank: list[BankStatementRow] = []

    for raw in sources["internal_orders"]:
        try:
            orders.append(InternalOrder.model_validate(raw))
        except ValidationError as error:
            raise ValueError(f"Invalid internal order {raw!r}: {error}") from error

    for raw in sources["payment_gateway"]:
        try:
            gateway.append(PaymentGatewaySettlement.model_validate(raw))
        except ValidationError as error:
            raise ValueError(
                f"Invalid gateway settlement {raw!r}: {error}"
            ) from error

    for raw in sources["bank_statement"]:
        try:
            bank.append(BankStatementRow.model_validate(raw))
        except ValidationError as error:
            raise ValueError(f"Invalid bank row {raw!r}: {error}") from error

    return {"internal_orders": orders, "payment_gateway": gateway, "bank_statement": bank}


def candidate_indexes(
    order: InternalOrder,
    gateway: list[PaymentGatewaySettlement],
    bank: list[BankStatementRow],
) -> tuple[list[PaymentGatewaySettlement], list[BankStatementRow]]:
    normalized = normalize_reference(order.order_reference)
    gateway_candidates = [
        record
        for record in gateway
        if normalize_reference(record.merchant_reference) == normalized
    ]
    bank_candidates = [
        record for record in bank if normalize_reference(record.reference) == normalized
    ]
    return gateway_candidates, bank_candidates


def deterministic_match(
    order: InternalOrder,
    gateway: list[PaymentGatewaySettlement],
    bank: list[BankStatementRow],
) -> tuple[PaymentGatewaySettlement | None, BankStatementRow | None, str | None]:
    gateway_candidates, bank_candidates = candidate_indexes(order, gateway, bank)
    order_amount = parse_decimal(order.amount)
    exact_gateway = [
        record for record in gateway_candidates if parse_decimal(record.gross_amount) == order_amount
    ]
    exact_bank = [
        record for record in bank_candidates if parse_decimal(record.amount) == order_amount
    ]
    gateway_match = exact_gateway[0] if len(exact_gateway) == 1 else None
    bank_match = exact_bank[0] if len(exact_bank) == 1 else None
    if gateway_match and bank_match:
        return gateway_match, bank_match, "deterministic"
    return gateway_match, bank_match, None


def reconcile(
    sources: dict[str, list[dict[str, Any]]],
    resolver: LLMResolver,
    max_exceptions: int = DEFAULT_MAX_EXCEPTIONS,
) -> ReconciliationReport:
    if max_exceptions < 1:
        raise ValueError("max_exceptions must be at least 1")

    typed = validate_sources(sources)
    orders = typed["internal_orders"]
    gateway = typed["payment_gateway"]
    bank = typed["bank_statement"]

    matches: list[MatchedRecord] = []
    exceptions: list[UnmatchedException] = []
    llm_used = 0

    for order in orders:
        gateway_candidates, bank_candidates = candidate_indexes(order, gateway, bank)
        gateway_match, bank_match, method = deterministic_match(order, gateway, bank)
        decision: LLMDecision | None = None
        if method is None:
            decision = resolver.resolve(
                order.model_dump(),
                [g.model_dump() for g in gateway_candidates],
                [b.model_dump() for b in bank_candidates],
            )
            llm_used += 1
            if decision.accepted:
                gateway_match = next(
                    (
                        row
                        for row in gateway_candidates
                        if row.settlement_id == decision.gateway_id
                    ),
                    None,
                )
                bank_match = next(
                    (
                        row
                        for row in bank_candidates
                        if row.bank_transaction_id == decision.bank_id
                    ),
                    None,
                )
                method = "llm"

        if method and gateway_match and bank_match:
            matches.append(
                MatchedRecord(
                    transaction_id=order.transaction_id,
                    gateway_settlement_id=gateway_match.settlement_id,
                    bank_transaction_id=bank_match.bank_transaction_id,
                    status="matched",
                    method=method,  # type: ignore[arg-type]
                    confidence=1.0 if method == "deterministic" else decision.confidence,
                    amount=parse_decimal(order.amount),
                )
            )
        else:
            reason = decision.reason if decision else "No unique exact match was found."
            if len(exceptions) < max_exceptions:
                exceptions.append(
                    UnmatchedException(
                        transaction_id=order.transaction_id,
                        reason=reason,
                        gateway_candidates=[row.settlement_id for row in gateway_candidates],
                        bank_candidates=[row.bank_transaction_id for row in bank_candidates],
                        llm_confidence=None if decision is None else decision.confidence,
                    )
                )

    total_unmatched = len(orders) - len(matches)
    report = ReconciliationReport(
        summary={
            "internal_orders": len(orders),
            "gateway_rows": len(gateway),
            "bank_rows": len(bank),
            "successful_matches": len(matches),
            "unmatched_exceptions_returned": len(exceptions),
            "unmatched_exceptions_total": total_unmatched,
            "exceptions_truncated": total_unmatched > len(exceptions),
            "llm_resolutions_attempted": llm_used,
            "exception_limit": max_exceptions,
        },
        matches=matches,
        unmatched_exceptions=exceptions,
    )
    return report


def report_to_dict(report: ReconciliationReport) -> dict[str, Any]:
    return {
        "report_version": report.report_version,
        "summary": report.summary,
        "matches": [m.model_dump(mode="json") for m in report.matches],
        "unmatched_exceptions": [e.model_dump(mode="json") for e in report.unmatched_exceptions],
    }
