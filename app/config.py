"""Application settings, read once from the environment at startup."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration. Field names mirror the UPPER_SNAKE env vars."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # LLM provider API
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_price_input_per_mtok: float = 0.0
    llm_price_output_per_mtok: float = 0.0

    # Storage
    database_url: str = "sqlite:///./data/tickets.db"

    # Optional auth
    api_key: str = ""

    # Routing / worker
    default_queue: str = "general"
    worker_enabled: bool = True
    worker_poll_seconds: float = 2.0

    # Circuit breaker
    circuit_failure_threshold: int = 5
    circuit_cooldown_seconds: float = 60.0

    # Evals
    eval_regression_threshold: float = 0.02

    # Logging
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, constructed once and cached."""
    return Settings()
