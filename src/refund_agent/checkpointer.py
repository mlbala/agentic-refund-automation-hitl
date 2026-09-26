"""LangGraph Postgres checkpointer: where paused agents (interrupts) are saved.

Because checkpoints live in Postgres, a refund waiting for approval survives an app restart:
the next graph.invoke(Command(resume=...)) for the same thread_id picks up where it stopped.
"""

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import psycopg_url


def create_pool(database_url: str) -> ConnectionPool:
    return ConnectionPool(
        psycopg_url(database_url),
        max_size=5,
        open=True,
        # autocommit: setup() runs CREATE INDEX CONCURRENTLY, which can't run in a transaction.
        # prepare_threshold=0: no server-side prepared statements (safe behind PgBouncer).
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        check=ConnectionPool.check_connection,  # drop dead connections to the remote server
    )


def create_checkpointer(pool: ConnectionPool) -> PostgresSaver:
    return PostgresSaver(pool)
