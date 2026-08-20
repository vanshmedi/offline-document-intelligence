"""
Configuration. Loaded from settings.json at the workspace root, overridable
by PI_* environment variables.

Every field declared here is actually consumed somewhere in the codebase.
Unknown keys in settings.json are rejected rather than silently ignored, so a
typo or a stale key surfaces immediately instead of being quietly dropped.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

PACKAGE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = PACKAGE_DIR.parent
DEFAULT_SETTINGS_PATH = WORKSPACE_DIR / "settings.json"

_ENV_PREFIX = "PI_"


class Settings(BaseModel):
    # Reject unknown keys: a stale config key should fail loudly, not vanish.
    model_config = ConfigDict(extra="forbid")

    # -- LLM ---------------------------------------------------------------
    llm_provider: Literal["ollama", "openrouter", "openai", "null"] = Field(
        default="ollama",
        description=(
            "Which backend serves the gap-fill and generation calls. "
            "'ollama' runs fully offline on this machine; 'openrouter' and 'openai' "
            "are cloud APIs; 'null' runs the pipeline deterministically with no model."
        ),
    )
    llm_model: Optional[str] = Field(
        default=None,
        description=(
            "Explicit model override. Leave null to use the active provider's own "
            "default, so switching provider also switches to a model that exists there."
        ),
    )

    # Per-provider model defaults. A model tag is provider-specific --
    # 'qwen2.5:14b' means nothing to OpenRouter and 'anthropic/claude-sonnet-4'
    # means nothing to Ollama -- so each provider carries its own.
    ollama_model: str = "qwen2.5:14b"
    openrouter_model: str = "anthropic/claude-3.5-haiku"
    openai_model: str = "gpt-4o-mini"

    ollama_base_url: str = "http://localhost:11434"

    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_api_key_env: str = Field(
        default="OPENROUTER_API_KEY",
        description="Name of the env var holding the key. The key itself is never written to settings.json.",
    )
    openrouter_site_url: str = Field(
        default="https://github.com/product-intel",
        description="Sent as HTTP-Referer; OpenRouter uses it for attribution on your dashboard.",
    )
    openrouter_app_name: str = "Product Intelligence Engine"

    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key_env: str = Field(
        default="OPENAI_API_KEY",
        description="Name of the env var holding the key. The key itself is never stored in settings.",
    )

    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2
    llm_temperature: float = 0.0
    llm_enabled: bool = Field(
        default=True,
        description="Master switch. When false, only deterministic extraction runs.",
    )

    # -- Embeddings --------------------------------------------------------
    embedding_enabled: bool = True
    embedding_model: str = "BAAI/bge-m3"
    embedding_device: Literal["auto", "cpu", "cuda"] = "auto"
    embedding_batch_size: int = 64
    embedding_cache: bool = True

    # -- Storage -----------------------------------------------------------
    catalog_root: str = "Catalog"
    sources_root: str = "Sources"

    # -- Extraction --------------------------------------------------------
    deterministic_first: bool = Field(
        default=True,
        description="Run rule/table extraction before any LLM call. LLM only fills what remains.",
    )
    llm_gap_fill: bool = Field(
        default=True,
        description="Use the LLM to recover attributes the deterministic pass could not find.",
    )
    max_llm_fragments_per_product: int = 12

    # -- Quality gates -----------------------------------------------------
    review_confidence_threshold: float = Field(
        default=0.70,
        description="Attributes below this confidence are routed to the human review queue.",
    )
    publish_confidence_threshold: float = Field(
        default=0.85,
        description="Minimum mean confidence for a product to auto-publish.",
    )
    outlier_z_threshold: float = 3.5
    target_channel: Literal["core", "ecommerce", "enhanced"] = "ecommerce"

    # -- Enrichment --------------------------------------------------------
    enable_generation: bool = True
    enable_gap_fill_inheritance: bool = True
    generation_locales: List[str] = Field(default_factory=lambda: ["en-US"])

    # -- Runtime -----------------------------------------------------------
    max_workers: int = Field(default=4, description="Parallel source-processing workers.")
    log_level: str = "INFO"

    # -- Paths -------------------------------------------------------------
    @property
    def catalog_path(self) -> Path:
        p = Path(self.catalog_root)
        return p if p.is_absolute() else WORKSPACE_DIR / p

    @property
    def sources_path(self) -> Path:
        p = Path(self.sources_root)
        return p if p.is_absolute() else WORKSPACE_DIR / p

    @property
    def manifest_path(self) -> Path:
        return self.catalog_path / "manifest.json"

    @property
    def db_path(self) -> Path:
        return self.catalog_path / "catalog.db"

    @property
    def graph_path(self) -> Path:
        return self.catalog_path / "graph.json"

    @property
    def review_queue_path(self) -> Path:
        return self.catalog_path / "review_queue.json"

    @property
    def learned_rules_path(self) -> Path:
        return self.catalog_path / "learned_rules.json"

    @property
    def vector_index_path(self) -> Path:
        return self.catalog_path / "vector_index.json"

    @property
    def embedding_cache_path(self) -> Path:
        return self.catalog_path / ".embedding_cache.json"

    # -- LLM resolution -----------------------------------------------------

    @property
    def active_model(self) -> str:
        """
        The model tag actually sent to the active provider.

        An explicit `llm_model` wins; otherwise the provider's own default is
        used, so flipping the toggle never leaves you pointing a cloud API at an
        Ollama tag it has never heard of.
        """
        if self.llm_model:
            return self.llm_model
        return {
            "ollama": self.ollama_model,
            "openrouter": self.openrouter_model,
            "openai": self.openai_model,
        }.get(self.llm_provider, self.ollama_model)

    @property
    def is_offline(self) -> bool:
        """True when no request can leave this machine."""
        return not self.llm_enabled or self.llm_provider in ("ollama", "null")

    def api_key_env_for(self, provider: Optional[str] = None) -> Optional[str]:
        return {
            "openrouter": self.openrouter_api_key_env,
            "openai": self.openai_api_key_env,
        }.get(provider or self.llm_provider)

    def api_key(self, provider: Optional[str] = None) -> Optional[str]:
        """Read the key from the environment. Keys are never persisted to settings.json."""
        env_name = self.api_key_env_for(provider)
        if not env_name:
            return None
        value = os.environ.get(env_name, "").strip()
        return value or None

    def openrouter_api_key(self) -> Optional[str]:
        return self.api_key("openrouter")

    def openai_api_key(self) -> Optional[str]:
        return self.api_key("openai")


def _apply_env_overrides(data: dict) -> dict:
    """PI_LLM_MODEL=... overrides settings.json's llm_model."""
    for field_name, field in Settings.model_fields.items():
        env_key = f"{_ENV_PREFIX}{field_name.upper()}"
        if env_key not in os.environ:
            continue
        raw = os.environ[env_key]
        annotation = field.annotation
        try:
            if annotation is bool:
                data[field_name] = raw.strip().lower() in ("1", "true", "yes", "on")
            elif annotation is int:
                data[field_name] = int(raw)
            elif annotation is float:
                data[field_name] = float(raw)
            elif raw.strip().startswith(("[", "{")):
                data[field_name] = json.loads(raw)
            else:
                data[field_name] = raw
        except (ValueError, json.JSONDecodeError):
            data[field_name] = raw
    return data


DEFAULT_ENV_PATH = WORKSPACE_DIR / ".env"


def load_dotenv(path: Path = DEFAULT_ENV_PATH) -> int:
    """
    Load KEY=VALUE pairs from a .env file into the environment.

    API keys live here rather than in settings.json so the config file stays
    safe to commit. Existing environment variables always win, so a key set in
    the shell overrides the file. Returns how many variables were set.

    Deliberately dependency-free and forgiving: blank lines, `#` comments,
    `export ` prefixes and quoted values are all handled.
    """
    if not path.exists():
        return 0
    loaded = 0
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def load_settings(settings_path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    load_dotenv()
    data: dict = {}
    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.pop("_comment", None)
    data = _apply_env_overrides(data)
    return Settings(**data)


def save_settings(
    updates: dict,
    settings_path: Path = DEFAULT_SETTINGS_PATH,
) -> Settings:
    """
    Persist a partial settings update and refresh the module-level `settings`.

    Used by the provider toggle so the choice survives the process. Only the
    keys passed in are touched; everything else in settings.json is preserved,
    including its `_comment`. The updated object is validated before it is
    written, so an invalid toggle can never corrupt the file.
    """
    existing: dict = {}
    if settings_path.exists():
        with open(settings_path, "r", encoding="utf-8") as f:
            existing = json.load(f)

    comment = existing.pop("_comment", None)
    merged = {**existing, **updates}

    validated = Settings(**_apply_env_overrides(dict(merged)))  # raises before writing

    payload = dict(merged)
    if comment is not None:
        payload = {"_comment": comment, **payload}

    tmp = settings_path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)

    reload_settings()
    return validated


def reload_settings(settings_path: Path = DEFAULT_SETTINGS_PATH) -> Settings:
    """Re-read settings.json in place, so long-lived processes see a toggle."""
    global settings
    fresh = load_settings(settings_path)
    settings.__dict__.update(fresh.__dict__)
    return settings


settings = load_settings()
