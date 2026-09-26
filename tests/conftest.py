"""Test fixtures: in-memory SQLite, in-memory checkpointer and a scripted fake LLM.

No network, no real LLM and no Postgres are needed.
"""

import itertools
from decimal import Decimal

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from refund_agent import db
from refund_agent.graph import build_graph
from refund_agent.service import RefundService

THRESHOLD = Decimal("100.00")
_call_ids = itertools.count(1)


class ScriptedChatModel(GenericFakeChatModel):
    """Replays scripted AIMessages in order. Tool binding is a no-op."""

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, *args, **kwargs):
        try:
            return super()._generate(*args, **kwargs)
        except StopIteration:
            raise RuntimeError("scripted LLM has no more replies") from None


def call(tool_name: str, **args) -> AIMessage:
    """An AI message that requests one tool call."""
    tool_call = {"name": tool_name, "args": args, "id": f"call_{next(_call_ids)}", "type": "tool_call"}
    return AIMessage(content="", tool_calls=[tool_call])


def refund_script(invoice_id: str, amount: float, summary: str = "Here is the outcome.") -> list[AIMessage]:
    """The happy path: look up the invoice, issue the refund, summarise."""
    return [
        call("get_invoice", invoice_id=invoice_id),
        call("issue_refund", invoice_id=invoice_id, amount=amount, reason="Customer returned the item."),
        AIMessage(summary),
    ]


@pytest.fixture(autouse=True)
def no_tracing(monkeypatch):
    """Keep tests offline even if the developer's shell enables LangSmith tracing."""
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")


@pytest.fixture
def engine():
    engine = create_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    db.create_tables(engine)
    db.seed_invoices(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def checkpointer():
    return MemorySaver()


@pytest.fixture
def make_service(engine, checkpointer):
    """Build a RefundService whose LLM replays `script`.

    Every call builds a NEW graph on the same database and checkpointer, which is exactly
    what an app restart looks like.
    """

    def _make(script: list[AIMessage]) -> RefundService:
        llm = ScriptedChatModel(messages=iter(script))
        graph = build_graph(llm, checkpointer, engine, THRESHOLD)
        return RefundService(engine, graph, THRESHOLD)

    return _make


@pytest.fixture
def invoice_status(engine):
    def _status(invoice_id: str) -> str:
        with engine.connect() as conn:
            return db.get_invoice(conn, invoice_id)["payment_status"]

    return _status
