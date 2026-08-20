"""
Pluggable LLM provider.

Three backends behind one interface, switchable at runtime:

    ollama      local, offline, nothing leaves the machine (default)
    openrouter  cloud, one key for most hosted models
    openai      cloud, or any self-hosted OpenAI-compatible server
    null        no model at all

Switch with `product-intel llm use ollama|openrouter` or the toggle in the
Streamlit console. Because the choice is a runtime setting rather than a code
path, the same catalog can be built offline on a workstation and re-built
against a stronger cloud model without changing anything else.

NullProvider matters more than it looks. It lets the entire pipeline run with
no model at all, which is how the deterministic path is tested in CI and how
the system degrades when Ollama is down or a key is missing: extraction
coverage drops visibly in the scorecard, but nothing crashes and nothing
silently fabricates.
"""

from __future__ import annotations

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

import httpx

from product_intel.config import Settings, settings as global_settings

log = logging.getLogger(__name__)


class LLMUnavailable(RuntimeError):
    """Raised when a provider cannot serve a request. Callers degrade, never fabricate."""


class LLMConfigurationError(LLMUnavailable):
    """
    A problem retrying cannot fix: no API key, unknown model, no credit.

    Separated so the retry loop fails fast instead of sleeping through three
    attempts at a request that was never going to succeed.
    """


class LLMProvider(ABC):
    name: str = "base"

    def __init__(self, cfg: Optional[Settings] = None):
        self.cfg = cfg or global_settings
        self.call_count = 0
        self.total_latency_ms = 0

    @property
    def available(self) -> bool:
        return True

    @abstractmethod
    def _complete(self, prompt: str, json_mode: bool, system: Optional[str]) -> str: ...

    def complete(self, prompt: str, json_mode: bool = False, system: Optional[str] = None) -> str:
        if not self.cfg.llm_enabled:
            raise LLMUnavailable("LLM disabled by configuration (llm_enabled=false).")
        last_err: Optional[Exception] = None
        for attempt in range(self.cfg.llm_max_retries + 1):
            started = time.time()
            try:
                out = self._complete(prompt, json_mode, system)
                self.call_count += 1
                self.total_latency_ms += int((time.time() - started) * 1000)
                return out
            except LLMConfigurationError:
                raise  # misconfiguration: retrying changes nothing
            except Exception as exc:  # noqa: BLE001 - provider errors are heterogeneous
                last_err = exc
                if attempt < self.cfg.llm_max_retries:
                    time.sleep(0.6 * (attempt + 1))
        raise LLMUnavailable(f"{self.name} failed after retries: {last_err}")

    def complete_json(
        self,
        prompt: str,
        system: Optional[str] = None,
        expect: str = "object",
    ) -> Any:
        """Complete and parse JSON, repairing truncated or fenced output."""
        raw = self.complete(prompt, json_mode=True, system=system)
        parsed = parse_json_lenient(raw)
        if parsed is None:
            raise LLMUnavailable(f"{self.name} returned unparseable JSON: {raw[:180]}")
        if expect == "list" and isinstance(parsed, dict):
            for key in ("items", "results", "attributes", "data"):
                if isinstance(parsed.get(key), list):
                    return parsed[key]
            return [parsed]
        return parsed

    def stats(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": getattr(self.cfg, "active_model", None),
            "calls": self.call_count,
            "total_latency_ms": self.total_latency_ms,
            "mean_latency_ms": int(self.total_latency_ms / self.call_count) if self.call_count else 0,
        }


class NullProvider(LLMProvider):
    """No model. Every call raises, so callers fall back to deterministic paths."""

    name = "null"

    @property
    def available(self) -> bool:
        return False

    def _complete(self, prompt: str, json_mode: bool, system: Optional[str]) -> str:
        raise LLMUnavailable("NullProvider: no language model configured.")


class OllamaProvider(LLMProvider):
    """Local Ollama. Default, and the reason this runs air-gapped."""

    name = "ollama"

    def __init__(self, cfg: Optional[Settings] = None):
        super().__init__(cfg)
        self._client = httpx.Client(timeout=self.cfg.llm_timeout_seconds)
        self._available: Optional[bool] = None

    @property
    def available(self) -> bool:
        if self._available is None:
            try:
                r = httpx.get(f"{self.cfg.ollama_base_url}/api/tags", timeout=3.0)
                self._available = r.status_code == 200
            except Exception:  # noqa: BLE001
                self._available = False
        return self._available

    def _complete(self, prompt: str, json_mode: bool, system: Optional[str]) -> str:
        payload: Dict[str, Any] = {
            "model": self.cfg.active_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.cfg.llm_temperature},
        }
        if json_mode:
            payload["format"] = "json"
        if system:
            payload["system"] = system
        r = self._client.post(f"{self.cfg.ollama_base_url}/api/generate", json=payload)
        r.raise_for_status()
        return (r.json().get("response") or "").strip()


class _ChatCompletionsProvider(LLMProvider):
    """
    Shared implementation for any OpenAI-compatible /chat/completions endpoint.

    OpenAI, OpenRouter, vLLM, LM Studio and TGI all speak this protocol, so the
    only differences worth subclassing are the base URL, the key, and any
    provider-specific headers.
    """

    name = "chat-completions"
    provider_key = "openai"

    def __init__(self, cfg: Optional[Settings] = None):
        super().__init__(cfg)
        self._client = httpx.Client(timeout=self.cfg.llm_timeout_seconds)

    # -- subclass hooks ----------------------------------------------------

    def base_url(self) -> str:
        return self.cfg.openai_base_url

    def extra_headers(self) -> Dict[str, str]:
        return {}

    # ----------------------------------------------------------------------

    @property
    def available(self) -> bool:
        return bool(self.cfg.api_key(self.provider_key))

    def _complete(self, prompt: str, json_mode: bool, system: Optional[str]) -> str:
        key = self.cfg.api_key(self.provider_key)
        if not key:
            env_name = self.cfg.api_key_env_for(self.provider_key)
            raise LLMConfigurationError(
                f"No API key found. Set {env_name} in your environment or in the .env "
                f"file at the project root."
            )

        messages = ([{"role": "system", "content": system}] if system else []) + [
            {"role": "user", "content": prompt}
        ]
        payload: Dict[str, Any] = {
            "model": self.cfg.active_model,
            "messages": messages,
            "temperature": self.cfg.llm_temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {key}", **self.extra_headers()}
        r = self._client.post(f"{self.base_url()}/chat/completions", json=payload, headers=headers)

        # Surface the provider's own error text: "insufficient credits" or
        # "model not found" is far more actionable than a bare 402/404.
        if r.status_code >= 400:
            detail = ""
            try:
                body = r.json()
                detail = body.get("error", {}).get("message") or str(body)[:200]
            except Exception:  # noqa: BLE001
                detail = r.text[:200]
            # 401/403/404/402 are configuration problems; 429/5xx are worth a retry.
            error_cls = (
                LLMConfigurationError
                if r.status_code in (400, 401, 402, 403, 404)
                else LLMUnavailable
            )
            raise error_cls(f"{self.name} HTTP {r.status_code}: {detail}")

        data = r.json()
        choices = data.get("choices") or []
        if not choices:
            raise LLMUnavailable(f"{self.name} returned no choices: {str(data)[:200]}")
        return (choices[0].get("message", {}).get("content") or "").strip()


class OpenAIProvider(_ChatCompletionsProvider):
    """OpenAI, or any self-hosted OpenAI-compatible server."""

    name = "openai"
    provider_key = "openai"


class OpenRouterProvider(_ChatCompletionsProvider):
    """
    OpenRouter: one API key and one endpoint in front of most hosted models.

    Chosen as the cloud option because it means the toggle switches *provider*,
    not integration -- swapping between Claude, GPT, Gemini and hosted Llama is
    a model-name change, with no further code or credentials.
    """

    name = "openrouter"
    provider_key = "openrouter"

    def base_url(self) -> str:
        return self.cfg.openrouter_base_url

    def extra_headers(self) -> Dict[str, str]:
        # Optional, and used only for attribution on the OpenRouter dashboard.
        return {
            "HTTP-Referer": self.cfg.openrouter_site_url,
            "X-Title": self.cfg.openrouter_app_name,
        }


#: Models known to work well for schema-directed extraction: strong instruction
#: following, reliable JSON, and cheap enough to run over a whole catalog.
OPENROUTER_SUGGESTED_MODELS = [
    ("anthropic/claude-3.5-haiku", "Fast and cheap, very reliable JSON. Good default."),
    ("anthropic/claude-sonnet-4", "Highest extraction accuracy; costs more."),
    ("openai/gpt-4o-mini", "Cheap, solid structured output."),
    ("google/gemini-2.0-flash-001", "Very fast, very cheap, long context."),
    ("meta-llama/llama-3.3-70b-instruct", "Open-weights; lowest cost per token."),
    ("qwen/qwen-2.5-72b-instruct", "Open-weights, strong on technical text."),
]

OLLAMA_SUGGESTED_MODELS = [
    ("qwen2.5:14b", "Good accuracy/speed balance. Needs ~10 GB VRAM."),
    ("qwen2.5:7b", "Lighter; fine for gap-fill. ~5 GB VRAM."),
    ("llama3.1:8b", "Widely available general-purpose fallback."),
    ("mistral-nemo:12b", "Strong JSON adherence for its size."),
]


_PROVIDERS = {
    "ollama": OllamaProvider,
    "openrouter": OpenRouterProvider,
    "openai": OpenAIProvider,
    "null": NullProvider,
}


def get_provider(cfg: Optional[Settings] = None) -> LLMProvider:
    cfg = cfg or global_settings
    if not cfg.llm_enabled:
        return NullProvider(cfg)
    return _PROVIDERS.get(cfg.llm_provider, NullProvider)(cfg)


# ---------------------------------------------------------------------------
# JSON recovery
#
# Small local models routinely emit fenced, truncated or trailing-comma JSON.
# This is the hardened repair path carried over from the predecessor project.
# ---------------------------------------------------------------------------


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
        if m:
            return m.group(1).strip()
        text = re.sub(r"^```(?:json)?", "", text).strip()
    if not text.startswith(("{", "[")):
        m = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if m:
            return m.group(1).strip()
    return text


def _repair(text: str) -> str:
    """Close unbalanced quotes/brackets left by a truncated generation."""
    text = re.sub(r",\s*([}\]])", r"\1", text)  # trailing commas

    in_quote = False
    escaped = False
    stack: List[str] = []
    for ch in text:
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_quote = not in_quote
            continue
        if in_quote:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
                stack.pop()

    if in_quote:
        text += '"'
    text = text.rstrip()
    while text.endswith(","):
        text = text[:-1].rstrip()
    if text.endswith(":"):
        text += " null"
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"
    return text


def parse_json_lenient(text: str) -> Any:
    if not text or not text.strip():
        return None
    cleaned = _strip_fences(text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    try:
        return json.loads(_repair(cleaned))
    except json.JSONDecodeError:
        log.debug("JSON repair failed for: %s", cleaned[:200])
        return None
