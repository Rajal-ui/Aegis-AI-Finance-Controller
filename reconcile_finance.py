"""Reconcile internal orders with payment and bank reports.

CLI entry point for the AI Finance Controller. The core engine lives in
``src.reconcile``; this module wires up argument parsing, source loading,
resolver selection, and report output.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from urllib import request

from src.reconcile import (
    DEFAULT_MAX_EXCEPTIONS,
    HeuristicLLMResolver,
    LLMDecision,
    LLMResolver,
    load_jsonl,
    reconcile,
    report_to_dict,
)


class OpenAICompatibleResolver:
    """Minimal HTTP adapter for an OpenAI-compatible JSON chat endpoint."""

    def __init__(self, endpoint: str, api_key: str, model: str) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model

    def resolve(
        self,
        order: dict[str, Any],
        gateway_candidates: list[dict[str, Any]],
        bank_candidates: list[dict[str, Any]],
    ) -> LLMDecision:
        prompt = {
            "order": order,
            "gateway_candidates": gateway_candidates,
            "bank_candidates": bank_candidates,
            "instruction": "Return JSON with accepted, gateway_id, bank_id, confidence, reason. Accept only if both unique candidates represent the same transaction and amounts exactly agree.",
        }
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": json.dumps(prompt)}],
            }
        ).encode("utf-8")
        http_request = request.Request(
            self.endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Default resolver is an offline heuristic test double. "
            "Use --llm-mode openai with OPENAI_COMPATIBLE_ENDPOINT and "
            "OPENAI_API_KEY for a real model."
        ),
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("synthetic_data"),
        help="Directory containing the three JSONL source files.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reconciliation_report.json"),
        help="Path to write the JSON reconciliation report.",
    )
    parser.add_argument(
        "--max-exceptions",
        type=int,
        default=DEFAULT_MAX_EXCEPTIONS,
        help="Maximum number of unmatched exceptions to return (bounded output).",
    )
    parser.add_argument(
        "--llm-mode",
        choices=("heuristic", "openai"),
        default="heuristic",
        help="Resolver backend: offline heuristic or OpenAI-compatible endpoint.",
    )
    return parser


def main() -> None:
    parser = build_parser()
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
            raise SystemExit(
                "openai mode requires OPENAI_COMPATIBLE_ENDPOINT and OPENAI_API_KEY"
            )
        resolver: LLMResolver = OpenAICompatibleResolver(endpoint, api_key, model)
    else:
        resolver = HeuristicLLMResolver()

    report = reconcile(sources, resolver, max_exceptions=args.max_exceptions)
    args.output.write_text(
        json.dumps(report_to_dict(report), indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report.summary, indent=2))


if __name__ == "__main__":
    main()
