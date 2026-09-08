"""
Centralized application configuration.

Every tunable value in Mnemosyne is defined here and sourced from
environment variables (or a .env file) — nothing else in the codebase
should hardcode a limit, a timeout, or a connection string. See
DECISIONS.md, "Configuration strategy".
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MNEMOSYNE_", extra="ignore", frozen=True
    )

    app_env: Literal["development", "testing", "production"] = "development"

    database_url: str = "sqlite:///./mnemosyne.db"
    sql_echo: bool = False

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    api_host: str = "0.0.0.0"
    api_port: int = 8420

    max_title_length: int = 500
    max_summary_length: int = 2000
    max_tags_per_memory: int = 20
    max_tag_length: int = 50
    max_metadata_bytes: int = 65536

    default_page_size: int = 20
    max_page_size: int = 200


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached Settings instance. Tests should construct
    `Settings(...)` directly with explicit overrides rather than
    mutating this cached singleton."""
    return Settings()
