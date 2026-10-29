"""Configuration for LLM resolver backends.

All values are read from environment variables so no secrets are hardcoded.
Free provider options:

* Ollama (local, free, no API key): OLLAMA_ENDPOINT=http://localhost:11434/v1/chat/completions
* Groq (free tier): GROQ_API_KEY=... with OPENAI_COMPATIBLE_ENDPOINT=https://api.groq.com/openai/v1/chat/completions
* Together AI (free tier): TOGETHER_API_KEY=... with endpoint https://api.together.xyz/v1/chat/completions
* OpenRouter (free models): OPENROUTER_API_KEY=... with endpoint https://openrouter.ai/api/v1/chat/completions
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LLMConfig:
    mode: str
    endpoint: str | None
    api_key: str | None
    model: str

    @classmethod
    def from_env(cls, mode: str = "heuristic") -> "LLMConfig":
        if mode == "ollama":
            endpoint = os.environ.get(
                "OLLAMA_ENDPOINT", "http://localhost:11434/v1/chat/completions"
            )
            api_key = os.environ.get("OLLAMA_API_KEY", "ollama")
            model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
            return cls(mode="ollama", endpoint=endpoint, api_key=api_key, model=model)

        if mode == "openai":
            endpoint = os.environ.get("OPENAI_COMPATIBLE_ENDPOINT")
            api_key = os.environ.get("OPENAI_API_KEY")
            model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
            if not endpoint or not api_key:
                raise ValueError(
                    "openai mode requires OPENAI_COMPATIBLE_ENDPOINT and OPENAI_API_KEY"
                )
            return cls(mode="openai", endpoint=endpoint, api_key=api_key, model=model)

        return cls(mode="heuristic", endpoint=None, api_key=None, model="heuristic")
