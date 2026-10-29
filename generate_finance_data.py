"""Generate reproducible synthetic data for a three-source reconciliation job."""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any


RECORD_COUNT = 100


def money(value: Decimal) -> float:
    return float(value.quantize(Decimal("0.01")))


def build_sources(seed: int, record_count: int = RECORD_COUNT) -> dict[str, list[dict[str, Any]]]:
    """Build three source reports with controlled, documented discrepancies."""
    rng = random.Random(seed)
    start_date = date(2026, 1, 1)
    orders: list[dict[str, Any]] = []
    gateway: list[dict[str, Any]] = []
    bank: list[dict[str, Any]] = []

    delayed_settlements = {7, 21, 42, 73}
    partial_settlements = {13, 34, 65, 88}
    formatting_errors = {4, 29, 54, 79}
    missing_gateway = {96}
    missing_bank = {91, 97}

    for index in range(1, record_count + 1):
        order_id = f"ORD-{index:04d}"
        order_date = start_date + timedelta(days=index - 1)
        amount = Decimal(str(rng.randrange(2500, 25000))) / Decimal("100")
        customer_id = f"CUST-{rng.randrange(1000, 9999)}"
        order = {
            "transaction_id": order_id,
            "order_date": order_date.isoformat(),
            "customer_id": customer_id,
            "amount": money(amount),
            "currency": "USD",
            "order_reference": order_id,
        }
        orders.append(order)

        if index not in missing_gateway:
            gateway_reference = order_id
            if index in formatting_errors:
                gateway_reference = f" {order_id.lower()} "
            gateway_amount = amount
            if index in partial_settlements:
                gateway_amount -= Decimal("0.50")
            settled_date = order_date + timedelta(days=2 if index in delayed_settlements else 1)
            gateway.append(
                {
                    "settlement_id": f"SET-{index:04d}",
                    "settled_at": settled_date.isoformat(),
                    "merchant_reference": gateway_reference,
                    "gross_amount": money(gateway_amount),
                    "currency": "USD",
                    "status": "settled",
                }
            )

        if index not in missing_bank:
            bank_reference = order_id
            if index in formatting_errors:
                bank_reference = order_id.replace("-", "")
            posted_date = order_date + timedelta(days=3 if index in delayed_settlements else 2)
            bank.append(
                {
                    "bank_transaction_id": f"BANK-{index:04d}",
                    "posted_at": posted_date.isoformat(),
                    "reference": bank_reference,
                    "description": f"Card settlement {order_id}",
                    "amount": money(amount),
                    "currency": "USD",
                    "transaction_type": "CREDIT",
                }
            )

    # Keep each report useful as a standalone 100-row source while making the
    # replacement rows obvious candidates for the exception queue.
    gateway.append(
        {
            "settlement_id": "SET-ORPHAN-0001",
            "settled_at": (start_date + timedelta(days=record_count + 1)).isoformat(),
            "merchant_reference": "ORD-9999",
            "gross_amount": 125.00,
            "currency": "USD",
            "status": "settled",
        }
    )
    bank.extend(
        [
            {
                "bank_transaction_id": "BANK-ORPHAN-0001",
                "posted_at": (start_date + timedelta(days=record_count + 2)).isoformat(),
                "reference": "ORD-9998",
                "description": "Card settlement ORD-9998",
                "amount": 214.75,
                "currency": "USD",
                "transaction_type": "CREDIT",
            },
            {
                "bank_transaction_id": "BANK-ORPHAN-0002",
                "posted_at": (start_date + timedelta(days=record_count + 3)).isoformat(),
                "reference": "ORD-9997",
                "description": "Card settlement ORD-9997",
                "amount": 87.50,
                "currency": "USD",
                "transaction_type": "CREDIT",
            },
        ]
    )
    return {"internal_orders": orders, "payment_gateway": gateway, "bank_statement": bank}


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, separators=(",", ":")) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=Path("synthetic_data"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    sources = build_sources(seed=args.seed)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for source_name, records in sources.items():
        write_jsonl(args.output_dir / f"{source_name}.jsonl", records)
    metadata = {
        "seed": args.seed,
        "internal_record_count": len(sources["internal_orders"]),
        "gateway_record_count": len(sources["payment_gateway"]),
        "bank_record_count": len(sources["bank_statement"]),
        "known_discrepancy_types": [
            "partial settlements",
            "delayed settlements",
            "minor reference formatting errors",
            "missing source rows represented by orphan replacements",
        ],
    }
    (args.output_dir / "README.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
