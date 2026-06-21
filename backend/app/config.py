"""
config.py
---------
Central configuration, loaded from environment variables / .env file.

Beginner notes:
- We use pydantic-settings so values are read from the OS environment OR a
  local `.env` file (see .env.example).
- Nothing here requires a paid API. If AI_PROVIDER stays "none", the system
  uses built-in rule-based logic.
- `settings` is a singleton you import everywhere:  from app.config import settings
"""

from __future__ import annotations

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Database ---
    database_url: str = "sqlite:///./data/jobs.db"

    # --- AI provider (optional) ---
    ai_provider: str = "none"  # none | anthropic | openai
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    anthropic_model: str = "claude-opus-4-8"
    openai_model: str = "gpt-4o-mini"
    # Model used by the on-demand resume builder. Cheaper than the notes/cover
    # model on purpose — reordering an existing résumé is a light task and this
    # runs once per job you choose to apply to. Override in .env (RESUME_MODEL).
    resume_model: str = "claude-haiku-4-5"

    # --- Crawler behavior ---
    crawl_delay_seconds: float = 1.0
    request_timeout_seconds: int = 20
    user_agent: str = "JobControlCenter/1.0 (+personal-job-search)"

    # --- Scoring ---
    # Canonical New/Best routing threshold. This in-code default is the single
    # source of truth; .env may override. (Was 75; lowered to 50 — see .env.)
    min_good_score: int = 50
    # A job needs at least this score to get resume notes / cover letter
    # generated. Shared by every routing call site to prevent drift.
    materials_min_score: int = 60
    # A job needs at least this score for the dashboard to offer the on-demand
    # "Build résumé" button. Lower than materials_min_score by design — you may
    # want a tailored résumé for a decent-but-not-Best match worth applying to.
    resume_min_score: int = 25

    # --- US-only search ---
    # When True, jobs whose location is clearly outside the US are hard-filtered.
    us_only: bool = True

    # --- Retention ---
    # Jobs posted more than this many days ago are pruned (and skipped at crawl
    # time) to keep the database light. Jobs you've actioned are always kept.
    prune_days: int = 10

    # --- Candidate profile ---
    my_skills: str = "python,sql,aws,etl,spark,airflow,docker"
    my_target_roles: str = "data engineer,cloud engineer,software engineer"
    my_work_auth: str = "F-1 OPT (STEM), needs H-1B sponsorship later"

    # --- Dashboard ---
    api_base_url: str = "http://127.0.0.1:8000"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignore unknown keys instead of crashing
    )

    # ---- Convenience helpers (parsed lists) ----
    @property
    def skills_list(self) -> List[str]:
        return [s.strip().lower() for s in self.my_skills.split(",") if s.strip()]

    @property
    def target_roles_list(self) -> List[str]:
        return [r.strip().lower() for r in self.my_target_roles.split(",") if r.strip()]

    @property
    def ai_enabled(self) -> bool:
        if self.ai_provider == "anthropic":
            return bool(self.anthropic_api_key)
        if self.ai_provider == "openai":
            return bool(self.openai_api_key)
        return False

    @property
    def resume_ai_enabled(self) -> bool:
        """The résumé builder uses Claude whenever an Anthropic key is present —
        independent of AI_PROVIDER, so you can turn on tailored résumés without
        also flipping notes/cover-letter generation to the API."""
        return bool(self.anthropic_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Import this everywhere.
settings = get_settings()
