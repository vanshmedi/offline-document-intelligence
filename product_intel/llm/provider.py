"""
Pluggable LLM provider.

Three backends behind one interface, switchable at runtime:

    ollama    local, offline, nothing leaves the machine (default)
    bedrock   AWS Bedrock, via the standard AWS credential chain
    null      no model at all

Switch with `product-intel llm use ollama|bedrock` or the toggle in the
web console. Because the choice is a runtime setting rather than a code
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


class BedrockProvider(LLMProvider):
    """
    AWS Bedrock via the Converse API.

    Converse rather than InvokeModel because it presents one uniform request
    and response shape across Anthropic, Amazon Nova, Meta and Mistral models.
    Switching model families becomes a model-ID change with no code change,
    which is the whole point of having a provider abstraction.

    Credentials come from the standard boto3 chain -- environment variables, a
    named profile, ~/.aws/credentials, or an EC2/ECS/EKS role. Nothing secret
    is ever read from or written to settings.json, so the same config file is
    safe to commit and works unchanged on a developer laptop and in a task role.
    """

    name = "bedrock"

    def __init__(self, cfg: Optional[Settings] = None):
        super().__init__(cfg)
        self._client = None
        self._client_error: Optional[str] = None

    # -- client ------------------------------------------------------------

    def _get_client(self):
        """Build the bedrock-runtime client once, converting setup failures into clear errors."""
        if self._client is not None:
            return self._client
        if self._client_error is not None:
            raise LLMConfigurationError(self._client_error)

        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as exc:
            self._client_error = (
                "boto3 is not installed. Run: pip install boto3   "
                f"(underlying error: {exc})"
            )
            raise LLMConfigurationError(self._client_error) from exc

        try:
            session = (
                boto3.Session(profile_name=self.cfg.aws_profile)
                if self.cfg.aws_profile
                else boto3.Session()
            )
            if session.get_credentials() is None:
                self._client_error = (
                    "No AWS credentials found. Set "
                    f"${self.cfg.aws_access_key_id_env} and ${self.cfg.aws_secret_access_key_env} "
                    "in the .env file at the project root, or configure a profile with "
                    "`aws configure`."
                )
                raise LLMConfigurationError(self._client_error)

            self._client = session.client(
                "bedrock-runtime",
                region_name=self.cfg.aws_region,
                config=BotoConfig(
                    read_timeout=self.cfg.llm_timeout_seconds,
                    connect_timeout=15,
                    # boto3 has its own retry logic; ours sits above it, so keep
                    # this low to avoid multiplying the two together.
                    retries={"max_attempts": 2, "mode": "standard"},
                ),
            )
            return self._client
        except LLMConfigurationError:
            raise
        except Exception as exc:  # noqa: BLE001 - botocore raises a wide range
            self._client_error = f"Could not create a Bedrock client: {exc}"
            raise LLMConfigurationError(self._client_error) from exc

    @property
    def available(self) -> bool:
        return self.cfg.aws_credentials_present()

    # -- completion --------------------------------------------------------

    def _complete(self, prompt: str, json_mode: bool, system: Optional[str]) -> str:
        client = self._get_client()

        system_blocks = []
        if system:
            system_blocks.append({"text": system})
        if json_mode:
            # Converse has no response_format switch, so JSON is requested in the
            # system block. The lenient parser downstream handles any preamble a
            # model adds anyway, so this is a nudge rather than a guarantee.
            system_blocks.append(
                {"text": "Respond with a single valid JSON value and nothing else. "
                         "No prose, no explanation, no markdown code fences."}
            )

        request: Dict[str, Any] = {
            "modelId": self.cfg.active_model,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {
                "temperature": self.cfg.llm_temperature,
                "maxTokens": self.cfg.bedrock_max_tokens,
            },
        }
        if system_blocks:
            request["system"] = system_blocks

        try:
            response = client.converse(**request)
        except Exception as exc:  # noqa: BLE001 - botocore ClientError hierarchy
            raise self._translate_error(exc) from exc

        try:
            blocks = response["output"]["message"]["content"]
        except (KeyError, TypeError) as exc:
            raise LLMUnavailable(f"Unexpected Bedrock response shape: {str(response)[:200]}") from exc

        text = "".join(b.get("text", "") for b in blocks).strip()
        if not text:
            stop = response.get("stopReason", "unknown")
            raise LLMUnavailable(f"Bedrock returned no text (stopReason={stop}).")
        return text

    def _translate_error(self, exc: Exception) -> LLMUnavailable:
        """
        Turn a botocore error into something a human can act on.

        Bedrock's failure modes are specific and each has a specific fix, so a
        bare ClientError string is a wasted opportunity to tell the user what
        to do next.
        """
        code = ""
        message = str(exc)
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            error = response.get("Error", {})
            code = error.get("Code", "") or ""
            message = error.get("Message", message) or message

        region = self.cfg.aws_region
        model = self.cfg.active_model

        permanent = {
            "AccessDeniedException": (
                f"Access denied for '{model}' in {region}. Enable the model in the Bedrock "
                f"console under Model access, and confirm the IAM identity has "
                f"bedrock:InvokeModel."
            ),
            "ResourceNotFoundException": (
                f"Model '{model}' was not found in {region}. Most current models are only "
                f"reachable through a cross-region inference profile (an ID beginning "
                f"'us.', 'eu.' or 'apac.'). Run `product-intel llm models` to list what is "
                f"available in your account."
            ),
            "ValidationException": f"Bedrock rejected the request: {message}",
            "UnrecognizedClientException": "The AWS credentials were rejected. Check the key and secret.",
            "InvalidSignatureException": (
                "AWS signature rejected. This usually means a mistyped secret key, or a "
                "machine clock that is out of sync."
            ),
        }
        if code in permanent:
            return LLMConfigurationError(f"{code}: {permanent[code]}")

        transient = ("ThrottlingException", "ModelTimeoutException",
                     "ServiceUnavailableException", "InternalServerException")
        if code in transient:
            return LLMUnavailable(f"{code}: {message} (transient -- will retry)")

        return LLMUnavailable(f"Bedrock call failed: {code or type(exc).__name__}: {message}")

    # -- discovery ---------------------------------------------------------

    def list_available_models(self) -> List[Dict[str, str]]:
        """
        Ask the account which text models it can actually invoke.

        Far more reliable than a hardcoded list: model availability varies by
        region and by which models the account has enabled, and IDs change.
        """
        try:
            import boto3
        except ImportError as exc:
            raise LLMConfigurationError("boto3 is not installed. Run: pip install boto3") from exc

        session = (
            boto3.Session(profile_name=self.cfg.aws_profile)
            if self.cfg.aws_profile
            else boto3.Session()
        )
        control = session.client("bedrock", region_name=self.cfg.aws_region)

        out: List[Dict[str, str]] = []

        # Inference profiles first: for most current models this is the only
        # invocable ID, so listing foundation models alone would mislead.
        try:
            profiles = control.list_inference_profiles().get("inferenceProfileSummaries", [])
            for p in profiles:
                out.append({
                    "id": p.get("inferenceProfileId", ""),
                    "name": p.get("inferenceProfileName", ""),
                    "kind": "inference profile",
                })
        except Exception as exc:  # noqa: BLE001 - older regions lack this API
            log.debug("could not list inference profiles: %s", exc)

        try:
            models = control.list_foundation_models(byOutputModality="TEXT").get("modelSummaries", [])
            for m in models:
                if "ON_DEMAND" not in (m.get("inferenceTypesSupported") or []):
                    continue
                out.append({
                    "id": m.get("modelId", ""),
                    "name": f"{m.get('providerName', '')} {m.get('modelName', '')}".strip(),
                    "kind": "on-demand",
                })
        except Exception as exc:  # noqa: BLE001
            if not out:
                raise LLMUnavailable(f"Could not list Bedrock models: {exc}") from exc

        return out


#: Sensible starting points. Real availability depends on region and on which
#: models the account has enabled, so `llm models` queries AWS directly and
#: this list is only the fallback when that call is not possible.
BEDROCK_SUGGESTED_MODELS = [
    ("us.anthropic.claude-3-5-haiku-20241022-v1:0", "Fast and cheap, very reliable JSON. Good default."),
    ("us.anthropic.claude-sonnet-4-20250514-v1:0", "Highest extraction accuracy; costs more."),
    ("us.amazon.nova-lite-v1:0", "Amazon's cheapest capable text model."),
    ("us.amazon.nova-pro-v1:0", "Stronger Nova tier, still inexpensive."),
    ("us.meta.llama3-3-70b-instruct-v1:0", "Open-weights, low cost per token."),
    ("mistral.mistral-large-2407-v1:0", "Strong on technical text."),
]

OLLAMA_SUGGESTED_MODELS = [
    ("qwen2.5:14b", "Good accuracy/speed balance. Needs ~10 GB VRAM."),
    ("qwen2.5:7b", "Lighter; fine for gap-fill. ~5 GB VRAM."),
    ("llama3.1:8b", "Widely available general-purpose fallback."),
    ("mistral-nemo:12b", "Strong JSON adherence for its size."),
]


_PROVIDERS = {
    "ollama": OllamaProvider,
    "bedrock": BedrockProvider,
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
