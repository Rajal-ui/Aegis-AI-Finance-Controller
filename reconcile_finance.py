"""Reconcile internal orders with payment and bank reports.

CLI entry point for the AI Finance Controller. The core engine lives in
``src.reconcile``; this module wires up argument parsing, source loading,
resolver selection, and report output.

Supported LLM backends (free options first):

* ``heuristic`` — offline test double, no API key needed (default).
* ``ollama`` — local Ollama via OpenAI-compatible endpoint, free, no API key
  required. Set OLLAMA_ENDPOINT / OLLAMA_API_KEY / OLLAMA_MODEL to override.
* ``openai`` — any OpenAI-compatible endpoint (Groq, Together, OpenRouter, etc.
  free tiers). Requires OPENAI_COMPATIBLE_ENDPOINT and OPENAI_API_KEY.
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from pathlib import Path
from typing import Any

from src.config import LLMConfig
from src.reconcile import (
    DEFAULT_MAX_EXCEPTIONS,
    HeuristicLLMResolver,
    LLMDecision,
    LLMResolver,
    OllamaResolver,
    load_jsonl,
    reconcile,
    report_to_dict,
)


class OpenAICompatibleResolver:
    """Minimal HTTP adapter for an OpenAI-compatible JSON chat endpoint."""

    def __init__(self, config: LLMConfig) -> None:
        self.config = config

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
                "model": self.config.model,
                "temperature": 0,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "user", "content": json.dumps(prompt)}],
            }
        ).encode("utf-8")
        http_request = urllib.request.Request(
            self.config.endpoint or "",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(http_request, timeout=30) as response:
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
            "Use --llm-mode ollama for a free local model, or --llm-mode openai "
            "with OPENAI_COMPATIBLE_ENDPOINT and OPENAI_API_KEY for a free-tier "
            "provider such as Groq, Together AI, or OpenRouter."
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
        choices=("heuristic", "ollama", "openai"),
        default="heuristic",
        help="Resolver backend: heuristic (offline), ollama (free local), or "
        "openai (OpenAI-compatible free-tier endpoint).",
    )
    return parser


def build_resolver(args: argparse.Namespace) -> LLMResolver:
    config = LLMConfig.from_env(mode=args.llm_mode)
    if args.llm_mode == "ollama":
        return OllamaResolver(config)
    if args.llm_mode == "openai":
        return OpenAICompatibleResolver(config)
    return HeuristicLLMResolver()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    sources = {
        "internal_orders": load_jsonl(args.input_dir / "internal_orders.jsonl"),
        "payment_gateway": load_jsonl(args.input_dir / "payment_gateway.jsonl"),
        "bank_statement": load_jsonl(args.input_dir / "bank_statement.jsonl"),
    }

    resolver = build_resolver(args)
    report = reconcile(sources, resolver, max_exceptions=args.max_exceptions)
    args.output.write_text(
        json.dumps(report_to_dict(report), indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report.summary, indent=2))


if __name__ == "__main__":
    main()
