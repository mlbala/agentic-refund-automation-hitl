"""Building the database URL from the DB_* settings."""

import pytest
from psycopg.conninfo import conninfo_to_dict
from sqlalchemy.engine import make_url

from refund_agent.config import build_database_url, sqlalchemy_url

DB_ENV = {
    "DB_HOST": "db.example.com",
    "DB_PORT": "6543",
    "DB_NAME": "refunds",
    "DB_USER": "refund_app",
    "DB_PASSWORD": "s3cret",
}


def test_url_is_built_from_db_settings():
    assert build_database_url(DB_ENV) == "postgresql://refund_app:s3cret@db.example.com:6543/refunds?sslmode=require"


def test_port_and_sslmode_have_defaults():
    env = {k: v for k, v in DB_ENV.items() if k != "DB_PORT"} | {"DB_SSLMODE": "verify-full"}

    parts = conninfo_to_dict(build_database_url(env))

    assert parts["port"] == "5432"
    assert parts["sslmode"] == "verify-full"


def test_special_characters_in_password_survive_both_drivers():
    password = "p@ss:w/rd#?%+ &=é"
    url = build_database_url(DB_ENV | {"DB_PASSWORD": password})

    assert conninfo_to_dict(url)["password"] == password  # psycopg (checkpointer)
    assert make_url(sqlalchemy_url(url)).password == password  # SQLAlchemy (app tables)


def test_missing_settings_are_named_without_leaking_values():
    with pytest.raises(RuntimeError, match="DB_HOST, DB_PASSWORD") as error:
        build_database_url({"DB_NAME": "refunds", "DB_USER": "refund_app"})
    assert "refund_app" not in str(error.value)


def test_database_url_overrides_db_settings():
    url = "postgresql://other:pw@other-host:5432/otherdb?sslmode=disable"

    assert build_database_url(DB_ENV | {"DATABASE_URL": url}) == url
