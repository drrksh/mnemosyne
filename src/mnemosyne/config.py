"""
Centralized application configuration.

Every tunable value in Mnemosyne is defined here and sourced from
environment variables (prefix `MNEMOSYNE_`), a `.env` file in the
working directory, or `<home>/config.env` — nothing else in the
codebase should hardcode a limit, a timeout, or a connection string.
See DECISIONS.md, "Configuration strategy".

Mnemosyne's memory belongs to a *person*, not to a project, so by
default everything lives under a per-user home directory
(`~/.mnemosyne`, override with `MNEMOSYNE_HOME`) rather than the
current working directory.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_home() -> Path:
    return Path(os.environ.get("MNEMOSYNE_HOME") or Path.home() / ".mnemosyne").expanduser()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MNEMOSYNE_", extra="ignore", frozen=True
    )

    app_env: Literal["development", "testing", "production"] = "development"

    #: Root for the local database, caches and config. See module docstring.
    home: Path = Field(default_factory=default_home)
    #: Defaults to `sqlite:///<home>/mnemosyne.db` when unset.
    database_url: str = ""
    sql_echo: bool = False
    #: Apply pending migrations automatically when a process opens the
    #: database (local mode convenience; disable for managed deployments
    #: that run `mnemosyne db upgrade` as an explicit release step).
    auto_migrate: bool = True

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    # --- API server -------------------------------------------------------
    #: Loopback by default: in `local` auth mode there is no
    #: authentication, so the server must not be reachable from the network.
    api_host: str = "127.0.0.1"
    api_port: int = 8420
    #: `local`: single user, no authentication (CLI, MCP, hooks, localhost
    #: API). `api_key`: every request must carry `Authorization: Bearer <key>`.
    auth_mode: Literal["local", "api_key"] = "local"
    local_user_id: str = "local"
    #: Refuse to start in `local` auth mode on a non-loopback host unless
    #: this is explicitly set. A safety rail, not a feature.
    allow_insecure_remote: bool = False
    #: Server-side repository analysis reads arbitrary paths on the server's
    #: filesystem; it is only ever allowed in local mode unless enabled here.
    allow_server_side_repo_analysis: bool = False
    cors_origins: list[str] = Field(default_factory=list)

    # --- Memory validation limits ----------------------------------------
    max_title_length: int = 500
    max_summary_length: int = 2000
    max_content_length: int = 50000
    max_tags_per_memory: int = 20
    max_tag_length: int = 50
    max_metadata_bytes: int = 65536

    default_page_size: int = 20
    max_page_size: int = 200

    # --- Retrieval ---------------------------------------------------------
    #: `hashing` (default): deterministic, offline, zero-dependency.
    #: `http`: any OpenAI-compatible /embeddings endpoint (OpenAI, Voyage,
    #: Ollama, vLLM, LM Studio...). `sentence-transformers`: local model.
    embedder: Literal["hashing", "http", "sentence-transformers"] = "hashing"
    embedding_dim: int = 512
    embedding_http_url: str = "https://api.openai.com/v1/embeddings"
    embedding_http_model: str = "text-embedding-3-small"
    embedding_http_api_key: Optional[SecretStr] = None
    embedding_http_timeout_seconds: float = 30.0
    embedding_local_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_batch_size: int = 64

    #: Weights of the hybrid ranking function (see retrieval/ranker.py).
    weight_relevance: float = 0.7
    weight_importance: float = 0.12
    weight_confidence: float = 0.08
    weight_recency: float = 0.07
    weight_scope: float = 0.13
    weight_reinforcement: float = 0.05
    #: Share of the relevance signal taken from embeddings (rest is BM25).
    semantic_share: float = 0.5
    #: Share of relevance taken from developer-concept overlap.
    concept_share: float = 0.1
    #: Memories with relevance below this are not "about" the query.
    min_relevance: float = 0.06
    recency_half_life_days: float = 90.0
    mmr_lambda: float = 0.8

    # --- Context compilation -----------------------------------------------
    context_default_max_tokens: int = 1500
    #: Claude Code caps hook `additionalContext` at 10,000 characters;
    #: stay safely under it.
    hook_max_chars: int = 9000
    hook_max_tokens: int = 1800
    #: Fraction of the budget reserved for always-on profile memories.
    context_profile_share: float = 0.45

    # --- Learning ----------------------------------------------------------
    learning_min_confidence: float = 0.55
    evidence_cap_per_memory: int = 50
    near_duplicate_threshold: float = 0.92
    repo_max_files: int = 4000
    repo_max_file_bytes: int = 400_000
    repo_max_files_per_language: int = 400
    git_max_commits: int = 500
    generalize_min_repos: int = 2
    generalize_min_agreement: float = 0.6
    #: Only repositories where the developer authored at least this share
    #: of recent commits count towards *personal* cross-repo generalization.
    generalize_min_author_share: float = 0.2

    # --- Optional LLM extraction -----------------------------------------
    llm_extraction: bool = False
    #: Optional explicit key; otherwise the Anthropic SDK resolves
    #: credentials itself (ANTHROPIC_API_KEY, `ant auth login` profile...).
    anthropic_api_key: Optional[SecretStr] = None
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    #: Server-side refusal fallback: "default" lets the API route a declined
    #: request to an appropriate model; "none" disables it.
    llm_fallbacks: Literal["default", "none"] = "default"
    llm_timeout_seconds: float = 60.0
    llm_max_transcript_chars: int = 60_000

    # ------------------------------------------------------------------
    @model_validator(mode="before")
    @classmethod
    def _resolve_paths(cls, data: Any) -> Any:
        if isinstance(data, dict):
            home = data.get("home") or default_home()
            data["home"] = Path(home).expanduser()
            if not data.get("database_url"):
                data["database_url"] = f"sqlite:///{data['home'] / 'mnemosyne.db'}"
        return data

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    def ensure_home(self) -> None:
        """Create the home directory with owner-only permissions: it holds
        a personal memory database."""
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.logs_dir.mkdir(parents=True, exist_ok=True, mode=0o700)


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached Settings instance. Tests should construct
    `Settings(...)` directly with explicit overrides rather than
    mutating this cached singleton."""
    home = default_home()
    return Settings(_env_file=(home / "config.env", ".env"))
