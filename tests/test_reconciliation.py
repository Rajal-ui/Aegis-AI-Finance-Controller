"""Test suite for the AI Finance Controller reconciliation engine."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import json
import pytest

from src.config import LLMConfig
from src.models import (
    BankStatementRow,
    InternalOrder,
    PaymentGatewaySettlement,
)
from src.reconcile import (
    HeuristicLLMResolver,
    candidate_indexes,
    deterministic_match,
    load_jsonl,
    normalize_reference,
    parse_decimal,
    reconcile,
    OllamaResolver,
)


def make_order(index: int = 1, amount: str = "100.00") -> InternalOrder:
    return InternalOrder(
        transaction_id=f"ORD-{index:04d}",
        order_date="2026-01-01",
        customer_id="CUST-0001",
        amount=amount,
        order_reference=f"ORD-{index:04d}",
    )


def make_gateway(index: int = 1, amount: str = "100.00", ref: str | None = None) -> PaymentGatewaySettlement:
    return PaymentGatewaySettlement(
        settlement_id=f"SET-{index:04d}",
        settled_at="2026-01-03T00:00:00",
        merchant_reference=ref or f"ORD-{index:04d}",
        gross_amount=amount,
    )


def make_bank(index: int = 1, amount: str = "100.00", ref: str | None = None) -> BankStatementRow:
    return BankStatementRow(
        bank_transaction_id=f"BANK-{index:04d}",
        posted_at="2026-01-04T00:00:00",
        reference=ref or f"ORD-{index:04d}",
        description="Card settlement",
        amount=amount,
    )


def test_normalize_reference_strips_punctuation_and_uppercases() -> None:
    assert normalize_reference(" ord-0013 ") == "ORD0013"
    assert normalize_reference("ORD-0013") == "ORD0013"
    assert normalize_reference("ord0013") == "ORD0013"


def test_parse_decimal_quantizes_to_cents() -> None:
    assert parse_decimal("100.006") == Decimal("100.01")
    assert parse_decimal("100") == Decimal("100.00")


def test_deterministic_match_exact() -> None:
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "100.00")]
    bank = [make_bank(1, "100.00")]
    g, b, method = deterministic_match(order, gateway, bank)
    assert method == "deterministic"
    assert g is not None and b is not None


def test_deterministic_match_partial_settlement_returns_none_method() -> None:
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "99.50")]
    bank = [make_bank(1, "100.00")]
    g, b, method = deterministic_match(order, gateway, bank)
    assert method is None
    assert g is None and b is not None


def test_deterministic_match_missing_bank() -> None:
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "100.00")]
    g, b, method = deterministic_match(order, gateway, [])
    assert method is None
    assert g is not None and b is None


def test_candidate_indexes_formatting_error() -> None:
    order = make_order(4, "70.72")
    gateway = [make_gateway(4, "70.72", ref=" ORD-0004 ")]
    bank = [make_bank(4, "70.72", ref="ORD0004")]
    g_cands, b_cands = candidate_indexes(order, gateway, bank)
    assert len(g_cands) == 1
    assert len(b_cands) == 1


def test_heuristic_llm_rejects_partial_variance() -> None:
    resolver = HeuristicLLMResolver()
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "99.50")]
    bank = [make_bank(1, "100.00")]
    decision = resolver.resolve(
        order.model_dump(), [g.model_dump() for g in gateway], [b.model_dump() for b in bank]
    )
    assert decision.accepted is False
    assert "0.50" in decision.reason


def test_heuristic_llm_accepts_exact_match() -> None:
    resolver = HeuristicLLMResolver()
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "100.00")]
    bank = [make_bank(1, "100.00")]
    decision = resolver.resolve(
        order.model_dump(), [g.model_dump() for g in gateway], [b.model_dump() for b in bank]
    )
    assert decision.accepted is True


def test_reconcile_end_to_end_matches_majority() -> None:
    orders = [make_order(i, "100.00") for i in range(1, 11)]
    gateway = [make_gateway(i, "100.00") for i in range(1, 11)]
    bank = [make_bank(i, "100.00") for i in range(1, 11)]
    sources = {
        "internal_orders": [o.model_dump() for o in orders],
        "payment_gateway": [g.model_dump() for g in gateway],
        "bank_statement": [b.model_dump() for b in bank],
    }
    report = reconcile(sources, HeuristicLLMResolver(), max_exceptions=20)
    assert report.summary["successful_matches"] == 10
    assert report.summary["unmatched_exceptions_total"] == 0
    assert report.summary["exceptions_truncated"] is False


def test_reconcile_exception_cap_truncation() -> None:
    orders = [make_order(i, "100.00") for i in range(1, 6)]
    gateway = []
    bank = []
    sources = {
        "internal_orders": [o.model_dump() for o in orders],
        "payment_gateway": [g.model_dump() for g in gateway],
        "bank_statement": [b.model_dump() for b in bank],
    }
    report = reconcile(sources, HeuristicLLMResolver(), max_exceptions=3)
    assert report.summary["unmatched_exceptions_returned"] == 3
    assert report.summary["unmatched_exceptions_total"] == 5
    assert report.summary["exceptions_truncated"] is True


def test_load_jsonl(tmp_path) -> None:
    p = tmp_path / "data.jsonl"
    p.write_text('{"a": 1}\n{"b": 2}\n', encoding="utf-8")
    rows = load_jsonl(p)
    assert rows == [{"a": 1}, {"b": 2}]


def test_model_validation_rejects_bad_order_id() -> None:
    with pytest.raises(Exception):
        InternalOrder(
            transaction_id="BAD-ID",
            order_date="2026-01-01",
            customer_id="CUST-0001",
            amount="100.00",
            order_reference="ORD-0001",
        )


# --- config tests ---


def test_config_from_env_heuristic_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_COMPATIBLE_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = LLMConfig.from_env(mode="heuristic")
    assert cfg.mode == "heuristic"
    assert cfg.endpoint is None
    assert cfg.api_key is None


def test_config_from_env_ollama_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OLLAMA_ENDPOINT", raising=False)
    monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_MODEL", raising=False)
    cfg = LLMConfig.from_env(mode="ollama")
    assert cfg.mode == "ollama"
    assert cfg.endpoint == "http://localhost:11434/v1/chat/completions"
    assert cfg.api_key == "ollama"
    assert cfg.model == "llama3.1:8b"


def test_config_from_env_openai_requires_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_COMPATIBLE_ENDPOINT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="requires OPENAI_COMPATIBLE_ENDPOINT"):
        LLMConfig.from_env(mode="openai")


# --- OllamaResolver tests ---


def fake_urlopen_openai_shape(payload: bytes, **kwargs: object) -> object:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "accepted": True,
                                        "gateway_id": "SET-0001",
                                        "bank_id": "BANK-0001",
                                        "confidence": 0.9,
                                        "reason": "Amounts match.",
                                    }
                                )
                            }
                        }
                    ]
                }
            ).encode("utf-8")

    return FakeResponse()


def fake_urlopen_ollama_shape(payload: bytes, **kwargs: object) -> object:
    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "accepted": True,
                                "gateway_id": "SET-0001",
                                "bank_id": "BANK-0001",
                                "confidence": 0.88,
                                "reason": "Amounts match.",
                            }
                        )
                    }
                }
            ).encode("utf-8")

    return FakeResponse()


def test_ollama_resolver_parses_openai_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://localhost:11434/v1/chat/completions")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    cfg = LLMConfig.from_env(mode="ollama")
    resolver = OllamaResolver(cfg)
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "100.00")]
    bank = [make_bank(1, "100.00")]
    with patch(
        "urllib.request.urlopen", side_effect=fake_urlopen_openai_shape
    ):
        decision = resolver.resolve(
            order.model_dump(mode="json"),
            [g.model_dump(mode="json") for g in gateway],
            [b.model_dump(mode="json") for b in bank],
        )
    assert decision.accepted is True
    assert decision.gateway_id == "SET-0001"
    assert decision.bank_id == "BANK-0001"
    assert decision.confidence == 0.9


def test_ollama_resolver_parses_ollama_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_ENDPOINT", "http://localhost:11434/v1/chat/completions")
    monkeypatch.setenv("OLLAMA_API_KEY", "ollama")
    monkeypatch.setenv("OLLAMA_MODEL", "llama3.1:8b")
    cfg = LLMConfig.from_env(mode="ollama")
    resolver = OllamaResolver(cfg)
    order = make_order(1, "100.00")
    gateway = [make_gateway(1, "100.00")]
    bank = [make_bank(1, "100.00")]
    with patch(
        "urllib.request.urlopen", side_effect=fake_urlopen_ollama_shape
    ):
        decision = resolver.resolve(
            order.model_dump(mode="json"),
            [g.model_dump(mode="json") for g in gateway],
            [b.model_dump(mode="json") for b in bank],
        )
    assert decision.accepted is True
    assert decision.confidence == 0.88
    assert decision.reason == "Amounts match."
