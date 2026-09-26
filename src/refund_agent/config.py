"""Settings loaded from environment variables (and a local .env file).

Secrets (DATABASE_URL, API keys) are read here and passed on, never printed or logged.
"""

import os
from dataclasses import dataclass
from decimal import Decimal

from dotenv import load_dotenv

DEFAULT_LLM_MODEL = "openai:gpt-5-mini"
DEFAULT_APPROVAL_THRESHOLD = "100"
CENTS = Decimal("0.01")


@dataclass(frozen=True)
class Settings:
    database_url: str
    llm_model: str
    approval_threshold: Decimal

    def __repr__(self) -> str:  # keep the connection string out of logs and tracebacks
        return (
            f"Settings(database_url='***', llm_model={self.llm_model!r}, "
            f"approval_threshold={self.approval_threshold})"
        )


def load_settings() -> Settings:
    load_dotenv()
    database_url = os.environ.get("DATABASE_URL", "").strip()
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set. Copy .env.example to .env and fill it in.")
    return Settings(
        database_url=database_url,
        llm_model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL,
        approval_threshold=to_money(
            os.environ.get("REFUND_APPROVAL_THRESHOLD", "").strip() or DEFAULT_APPROVAL_THRESHOLD
        ),
    )


def to_money(value) -> Decimal:
    """Convert a number (including an LLM-supplied float) to a 2-decimal Decimal."""
    return Decimal(str(value)).quantize(CENTS)


def sqlalchemy_url(database_url: str) -> str:
    """postgresql://... -> postgresql+psycopg://... so SQLAlchemy uses psycopg 3."""
    for prefix in ("postgresql://", "postgres://"):
        if database_url.startswith(prefix):
            return "postgresql+psycopg://" + database_url[len(prefix):]
    return database_url


def psycopg_url(database_url: str) -> str:
    """Plain libpq URI for psycopg (drops a SQLAlchemy driver suffix if present)."""
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)
