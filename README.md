# AI Finance Controller Foundation

This prototype generates three synthetic JSONL reports and reconciles them by internal order reference.

## Run it

```powershell
python generate_finance_data.py --output-dir synthetic_data --seed 42
python reconcile_finance.py --input-dir synthetic_data --output reconciliation_report.json
```

The generator creates 100 rows in each source. It includes delayed settlements, four 0.50 partial settlements, reference formatting differences, and missing rows represented by orphan replacements.

## Workflow

1. Load the internal order ledger, gateway settlement report, and bank statement.
2. Normalize references by uppercasing and removing punctuation.
3. Deterministically match only unique candidates whose amounts agree to cents.
4. Send incomplete or ambiguous cases to the `LLMResolver` boundary.
5. Accept an LLM result only when it identifies both source rows and exact amounts agree.
6. Emit successful matches and a bounded `unmatched_exceptions` list.

The default `HeuristicLLMResolver` is an offline test double with the same contract as the `OpenAICompatibleResolver`. To use a real LLM with a **free** API key:

```powershell
# Ollama (local, free, no API key required)
ollama serve
ollama pull llama3.1:8b
python reconcile_finance.py --llm-mode ollama

# Groq / Together AI / OpenRouter (free tiers, OpenAI-compatible)
$env:OPENAI_COMPATIBLE_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
$env:OPENAI_API_KEY = "gsk_..."
$env:OPENAI_MODEL = "llama-3.1-8b-instant"
python reconcile_finance.py --llm-mode openai
```

Configure via environment variables — no secrets are hardcoded:
- `OLLAMA_ENDPOINT`, `OLLAMA_API_KEY`, `OLLAMA_MODEL` (ollama mode)
- `OPENAI_COMPATIBLE_ENDPOINT`, `OPENAI_API_KEY`, `OPENAI_MODEL` (openai mode)

The exception list is capped with `--max-exceptions` (default `20`). The report also records `unmatched_exceptions_total` and whether truncation occurred, so an operator can distinguish a clean run from a bounded output.
