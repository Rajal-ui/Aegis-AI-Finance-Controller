"""Reconcile internal orders with payment and bank reports.

The LLM boundary is intentionally explicit: production code can implement
LLMResolver with a hosted model, while the default resolver is offline and
safe for local demonstrations.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from urllib import request


@dataclass(frozen=True)
class LLMDecision:
    accepted: bool
    gateway_id: str | None
    bank_id: str | None
    confidence: float
    reason: str


class LLMResolver(Protocol):
    def resolve(self, order: dict[str, Any], gateway_candidates: list[dict[str, Any]], bank_candidates: list[dict[str, Any]]) -> LLMDecision:
        ...


class HeuristicLLMResolver:
    """Offline stand-in with the same contract expected from a real LLM."""

    def resolve(self, order: dict[str, Any], gateway_candidates: list[dict[str, Any]], bank_candidates: list[dict[str, Any]]) -> LLMDecision:
        gateway = gateway_candidates[0] if len(gateway_candidates) == 1 else None
        bank = bank_candidates[0] if len(bank_candidates) == 1 else None
        order_amount = Decimal(str(order["amount"]))
        gateway_variance = None if gateway is None else abs(Decimal(str(gateway["gross_amount"])) - order_amount)
        bank_variance = None if bank is None else abs(Decimal(str(bank["amount"])) - order_amount)
        accepted = gateway is not None and bank is not None and gateway_variance == 0 and bank_variance == 0
        if gateway is None or bank is None:
            reason = "A source row is missing or has no unique candidate."
        elif gateway_variance != 0:
            reason = f"Gateway amount variance is {gateway_variance:.2f}; hold for review."
        elif bank_variance != 0:
            reason = f"Bank amount variance is {bank_variance:.2f}; hold for review."
        else:
            reason = "Unique candidate resolved after deterministic matching was inconclusive."
        return LLMDecision(accepted, _value(gateway, "settlement_id"), _value(bank, "bank_transaction_id"), 0.86 if accepted else 0.62, reason)


class OpenAICompatibleResolver:
    """Minimal HTTP adapter for an OpenAI-compatible JSON chat endpoint."""

    def __init__(self, endpoint: str, api_key: str, model: str) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    def resolve(self, order: dict[str, Any], gateway_candidates: list[dict[str, Any]], bank_candidates: list[dict[str, Any]]) -> LLMDecision:
        prompt = {
            "order": order,
            "gateway_candidates": gateway_candidates,
            "bank_candidates": bank_candidates,
            "instruction": "Return JSON with accepted, gateway_id, bank_id, confidence, reason. Accept only if both unique candidates represent the same transaction and amounts exactly agree.",
        }
        payload = json.dumps({
            "model": self.model,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "user", "content": json.dumps(prompt)}],
        }).encode("utf-8")
        http_request = request.Request(self.endpoint, data=payload, headers={
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        with request.urlopen(http_request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"]
        result = json.loads(content)
        return LLMDecision(
            accepted=bool(result.get("accepted", False)),
            gateway_id=result.get("gateway_id"),
            bank_id=result.get("bank_id"),
            confidence=float(result.get("confidence", 0)),
            reason=str(result.get("reason", "No reason returned by model.")),
        )


def _value(record: dict[str, Any] | None, key: str) -> str | None:
    return None if record is None else str(record[key])


def normalize_reference(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"Invalid monetary value: {value!r}") from error


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def candidate_indexes(order: dict[str, Any], records: list[dict[str, Any]], reference_key: str) -> list[dict[str, Any]]:
    normalized = normalize_reference(order["order_reference"])
    return [record for record in records if normalize_reference(str(record[reference_key])) == normalized]


def deterministic_match(order: dict[str, Any], gateway: list[dict[str, Any]], bank: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str | None]:
    gateway_candidates = candidate_indexes(order, gateway, "merchant_reference")
    bank_candidates = candidate_indexes(order, bank, "reference")
    order_amount = decimal(order["amount"])
    exact_gateway = [record for record in gateway_candidates if decimal(record["gross_amount"]) == order_amount]
    exact_bank = [record for record in bank_candidates if decimal(record["amount"]) == order_amount]
    gateway_match = exact_gateway[0] if len(exact_gateway) == 1 else None
    bank_match = exact_bank[0] if len(exact_bank) == 1 else None
    if gateway_match and bank_match:
        return gateway_match, bank_match, "deterministic"
    return gateway_match, bank_match, None


def reconcile(sources: dict[str, list[dict[str, Any]]], resolver: LLMResolver, max_exceptions: int = 20) -> dict[str, Any]:
    if max_exceptions < 1:
        raise ValueError("max_exceptions must be at least 1")
    matches: list[dict[str, Any]] = []
    exceptions: list[dict[str, Any]] = []
    llm_used = 0
    orders = sources["internal_orders"]
    gateway = sources["payment_gateway"]
    bank = sources["bank_statement"]

    for order in orders:
        gateway_candidates = candidate_indexes(order, gateway, "merchant_reference")
        bank_candidates = candidate_indexes(order, bank, "reference")
        gateway_match, bank_match, method = deterministic_match(order, gateway, bank)
        decision: LLMDecision | None = None
        if method is None:
            decision = resolver.resolve(order, gateway_candidates, bank_candidates)
            llm_used += 1
            if decision.accepted:
                gateway_match = next((row for row in gateway_candidates if row.get("settlement_id") == decision.gateway_id), None)
                bank_match = next((row for row in bank_candidates if row.get("bank_transaction_id") == decision.bank_id), None)
                method = "llm"

        if method and gateway_match and bank_match:
            matches.append({
                "transaction_id": order["transaction_id"],
                "gateway_settlement_id": gateway_match["settlement_id"],
                "bank_transaction_id": bank_match["bank_transaction_id"],
                "status": "matched",
                "method": method,
                "confidence": 1.0 if method == "deterministic" else decision.confidence,
                "amount": order["amount"],
            })
        else:
            reason = decision.reason if decision else "No unique exact match was found."
            if len(exceptions) < max_exceptions:
                exceptions.append({
                    "transaction_id": order["transaction_id"],
                    "type": "unmatched_exception",
                    "reason": reason,
                    "gateway_candidates": [row.get("settlement_id") for row in gateway_candidates],
                    "bank_candidates": [row.get("bank_transaction_id") for row in bank_candidates],
                    "llm_confidence": None if decision is None else decision.confidence,
                })

    report = {
        "report_version": "1.0",
        "summary": {
            "internal_orders": len(orders),
            "gateway_rows": len(gateway),
            "bank_rows": len(bank),
            "successful_matches": len(matches),
            "unmatched_exceptions_returned": len(exceptions),
            "unmatched_exceptions_total": len(orders) - len(matches),
            "exceptions_truncated": len(orders) - len(matches) > len(exceptions),
            "llm_resolutions_attempted": llm_used,
            "exception_limit": max_exceptions,
        },
        "matches": matches,
        "unmatched_exceptions": exceptions,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=Path("synthetic_data"))
    parser.add_argument("--output", type=Path, default=Path("reconciliation_report.json"))
    parser.add_argument("--max-exceptions", type=int, default=20)
    parser.add_argument("--llm-mode", choices=("heuristic", "openai"), default="heuristic")
    args = parser.parse_args()

    sources = {
        "internal_orders": load_jsonl(args.input_dir / "internal_orders.jsonl"),
        "payment_gateway": load_jsonl(args.input_dir / "payment_gateway.jsonl"),
        "bank_statement": load_jsonl(args.input_dir / "bank_statement.jsonl"),
    }
    if args.llm_mode == "openai":
        endpoint = os.environ.get("OPENAI_COMPATIBLE_ENDPOINT")
        api_key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
        if not endpoint or not api_key:
            raise SystemExit("openai mode requires OPENAI_COMPATIBLE_ENDPOINT and OPENAI_API_KEY")
        resolver: LLMResolver = OpenAICompatibleResolver(endpoint, api_key, model)
    else:
        resolver = HeuristicLLMResolver()

    report = reconcile(sources, resolver, max_exceptions=args.max_exceptions)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
