"""Optional AI helpers. Leash works with NO AI key; the AI only PROPOSES, plain code DECIDES."""
from __future__ import annotations

import json
import os
from typing import Optional, Protocol

DEFAULT_MODEL = "claude-sonnet-5-5"  # override with the LEASH_MODEL environment variable


class LLM(Protocol):
    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str: ...


class AnthropicLLM:
    """Calls Claude through the official `anthropic` package (reads ANTHROPIC_API_KEY)."""

    def __init__(self, model: Optional[str] = None, api_key: Optional[str] = None):
        import anthropic  # imported here so the rest of Leash works without the package

        self._client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model or os.environ.get("LEASH_MODEL", DEFAULT_MODEL)

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        response = self._client.messages.create(
            model=self.model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in response.content if getattr(block, "type", "") == "text")

class OpenAICompatibleLLM:
    """Connects to a provider offering an OpenAI-style chat API."""

    def __init__(self):
        self.api_key = os.environ["LEASH_AI_API_KEY"]
        self.base_url = os.environ["LEASH_AI_BASE_URL"].rstrip("/")
        self.model = os.environ["LEASH_AI_MODEL"]

        if not self.base_url.startswith("https://"):
            raise ValueError("LEASH_AI_BASE_URL must use HTTPS")

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        import httpx

        response = httpx.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "max_tokens": max_tokens,
            },
            timeout=60.0,
            follow_redirects=False,
        )

        if response.is_error:
            raise RuntimeError(
                f"AI provider returned HTTP {response.status_code}. "
                "Check your model, API key, account limits and provider settings."
            )

        data = response.json()
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str) or not content.strip():
            raise ValueError("The AI provider returned no text")
        return content


class FakeLLM:
    """For tests: returns the replies you give it, in order, and remembers what it was asked."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def complete(self, system: str, user: str, *, max_tokens: int = 800) -> str:
        self.calls.append((system, user))
        return self.replies.pop(0) if self.replies else ""


def get_llm() -> Optional[LLM]:
    """Use the configured compatible provider, or Claude, or plain rules."""
    if os.environ.get("LEASH_AI_API_KEY"):
        return OpenAICompatibleLLM()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None

    try:
        return AnthropicLLM()
    except Exception:
        return None


def extract_json(text: str) -> dict:
    """Pull the first {...} object out of a model reply, even if it added extra words."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("the AI reply did not contain a JSON object")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("the AI reply was not a JSON object")
    return data
