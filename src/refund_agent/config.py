"""Settings loaded from environment variables (and a local .env file).

Secrets (DB_PASSWORD, API keys, the built database URL) are read here and passed on,
never printed or logged.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

from dotenv import load_dotenv

DEFAULT_LLM_MODEL = "openai:gpt-5-mini"
DEFAULT_APPROVAL_THRESHOLD = "100"
DEFAULT_DB_PORT = "5432"
DEFAULT_DB_SSLMODE = "require"
REQUIRED_DB_VARS = ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD")
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
    return Settings(
        database_url=build_database_url(os.environ),
        llm_model=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL,
        approval_threshold=to_money(
            os.environ.get("REFUND_APPROVAL_THRESHOLD", "").strip() or DEFAULT_APPROVAL_THRESHOLD
        ),
    )


def build_database_url(env: Mapping[str, str]) -> str:
    """Build postgresql://user:password@host:port/dbname?sslmode=... from the DB_* variables.

    User, password and database name are percent-encoded, so passwords may contain any
    character (@ : / # % spaces ...). A full DATABASE_URL, if set, overrides the DB_* variables.
    """
    override = env.get("DATABASE_URL", "").strip()
    if override:
        return override

    host, name, user = (env.get(var, "").strip() for var in ("DB_HOST", "DB_NAME", "DB_USER"))
    password = env.get("DB_PASSWORD", "")  # not stripped: spaces may be part of it
    missing = [var for var, value in zip(REQUIRED_DB_VARS, (host, name, user, password)) if not value]
    if missing:
        raise RuntimeError(
            f"Missing database settings: {', '.join(missing)}. Copy .env.example to .env and fill them in."
        )
    port = env.get("DB_PORT", "").strip() or DEFAULT_DB_PORT
    if not port.isdigit():
        raise RuntimeError(f"DB_PORT must be a number, got {port!r}.")
    sslmode = env.get("DB_SSLMODE", "").strip() or DEFAULT_DB_SSLMODE
    return (
        f"postgresql://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}/"
        f"{quote(name, safe='')}?sslmode={quote(sslmode, safe='')}"
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
