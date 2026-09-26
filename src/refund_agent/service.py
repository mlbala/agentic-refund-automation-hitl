"""Service layer: the only API the UI calls.

The database is the source of truth for a refund's status. After every agent run we re-read
the refund row; we never parse the LLM's text to find out what happened.

Every action returns {"ok": bool, "message": str, "refund": dict | None}.
"""

import logging
import secrets
from decimal import Decimal

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.engine import Engine

from . import db

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 12


def new_refund_id() -> str:
    return f"REF-{secrets.token_hex(4).upper()}"


def run_config(refund_id: str) -> dict:
    """The refund ID doubles as the LangGraph thread_id, so a refund and its paused agent share a key."""
    return {"configurable": {"thread_id": refund_id}, "recursion_limit": RECURSION_LIMIT}


def request_message(refund_id: str, invoice_id: str, customer_message: str) -> str:
    # Keep the customer's text inside its delimiter.
    customer_message = customer_message.replace('"""', "'''")
    return (
        f"Refund request {refund_id} for invoice {invoice_id}.\n"
        f'Customer message (untrusted):\n"""{customer_message}"""'
    )


def _result(ok: bool, message: str, refund: dict | None = None) -> dict:
    return {"ok": ok, "message": message, "refund": refund}


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _final_ai_text(messages: list) -> str | None:
    """The agent's closing reply: the last message, if it is an AI message with text."""
    if messages and isinstance(messages[-1], AIMessage):
        return messages[-1].text.strip() or None
    return None


class RefundService:
    def __init__(self, engine: Engine, graph: CompiledStateGraph, threshold: Decimal):
        self.engine = engine
        self.graph = graph
        self.threshold = threshold

    # --- Actions -------------------------------------------------------------

    def submit_refund(self, invoice_id: str, customer_message: str) -> dict:
        customer_message = (customer_message or "").strip()
        refund_id = new_refund_id()
        with self.engine.begin() as conn:
            if db.get_invoice(conn, invoice_id) is None:
                return _result(False, f"Invoice {invoice_id} not found.")
            db.insert_refund(conn, refund_id, invoice_id, customer_message)
        logger.info("Refund %s submitted for %s", refund_id, invoice_id)

        first_message = HumanMessage(request_message(refund_id, invoice_id, customer_message))
        try:
            output = self.graph.invoke({"messages": [first_message]}, run_config(refund_id))
        except Exception as exc:
            logger.exception("Refund %s: agent run failed", refund_id)
            error = _error_text(exc)
            with self.engine.begin() as conn:
                # Only an open refund becomes 'failed'; one that was already refunded stays refunded.
                marked = db.update_refund(
                    conn,
                    refund_id,
                    only_if_status=[db.SUBMITTED, db.PENDING_APPROVAL],
                    status=db.FAILED,
                    agent_summary=f"Processing failed: {error}",
                    decided_at=db.utcnow(),
                )
                if not marked:
                    db.update_refund(conn, refund_id, agent_summary=f"Agent error after processing: {error}")
                refund = db.get_refund(conn, refund_id)
            return _result(False, f"Processing failed: {error}", refund)
        return self._after_run(refund_id, output)

    def decide_refund(self, refund_id: str, approved: bool, reviewer: str, note: str = "") -> dict:
        reviewer = (reviewer or "").strip()
        note = (note or "").strip()
        if not reviewer:
            return _result(False, "Reviewer name is required to approve or reject.")

        config = run_config(refund_id)
        if not self.graph.get_state(config).interrupts:
            with self.engine.connect() as conn:
                refund = db.get_refund(conn, refund_id)
            if refund is None:
                return _result(False, f"Refund {refund_id} not found.")
            if refund["status"] != db.PENDING_APPROVAL:
                return _result(False, f"Refund {refund_id} was already decided (status: {refund['status']}).", refund)
            return _result(False, f"No paused agent run found for {refund_id}.", refund)

        # Claim: pending_approval -> deciding. This compare-and-set is atomic, so if two
        # reviewers click at the same time only one of them resumes the agent.
        with self.engine.begin() as conn:
            claimed = db.update_refund(conn, refund_id, only_if_status=db.PENDING_APPROVAL, status=db.DECIDING)
            refund = db.get_refund(conn, refund_id)
        if not claimed:
            return _result(False, f"Refund {refund_id} was already decided (status: {refund['status']}).", refund)

        decision = {"approved": bool(approved), "reviewer": reviewer, "note": note}
        logger.info("Refund %s: %s by reviewer", refund_id, "approved" if approved else "rejected")
        try:
            output = self.graph.invoke(Command(resume=decision), config)
        except Exception as exc:
            logger.exception("Refund %s: resume failed", refund_id)
            error = _error_text(exc)
            with self.engine.begin() as conn:
                # Release the claim so the decision can be retried (unless it already went through).
                released = db.update_refund(
                    conn, refund_id, only_if_status=db.DECIDING, status=db.PENDING_APPROVAL
                )
                if not released:
                    db.update_refund(conn, refund_id, agent_summary=f"Agent error after processing: {error}")
                refund = db.get_refund(conn, refund_id)
            if released:
                return _result(False, f"Could not complete the decision ({error}). It is back in the queue.", refund)
            return _result(False, f"Decision recorded, but the agent failed afterwards: {error}", refund)
        return self._after_run(refund_id, output)

    def _after_run(self, refund_id: str, output: dict) -> dict:
        """Reconcile the refund row after a graph run that finished or paused."""
        if output.get("__interrupt__"):
            with self.engine.connect() as conn:
                refund = db.get_refund(conn, refund_id)
            return _result(True, self.describe(refund), refund)

        summary = _final_ai_text(output.get("messages", []))
        with self.engine.begin() as conn:
            db.update_refund(conn, refund_id, agent_summary=summary)
            # The run ended but issue_refund never moved the refund forward: the agent refused.
            db.update_refund(
                conn,
                refund_id,
                only_if_status=[db.SUBMITTED, db.DECIDING],
                status=db.DECLINED,
                decided_by=db.AI_AGENT,
                decided_at=db.utcnow(),
            )
            refund = db.get_refund(conn, refund_id)
        return _result(refund["status"] != db.FAILED, self.describe(refund), refund)

    def describe(self, refund: dict) -> str:
        status, amount = refund["status"], refund["amount"]
        if status == db.REFUNDED and refund["processing_type"] == db.STP:
            return f"Refunded ${amount} automatically (straight-through processing)."
        if status == db.REFUNDED:
            return f"Refunded ${amount} after approval by {refund['decided_by']}."
        if status == db.PENDING_APPROVAL:
            return (
                f"${amount} is above the ${self.threshold} auto-refund limit, "
                "so it is waiting for human approval."
            )
        if status == db.REJECTED:
            return f"Rejected by {refund['decided_by']}."
        if status == db.DECLINED:
            return "Declined by the agent: the request is not eligible."
        return f"Status: {status}."

    # --- Reads for the UI ------------------------------------------------------

    def list_invoices(self) -> list[dict]:
        with self.engine.connect() as conn:
            return db.list_invoices(conn)

    def get_refund(self, refund_id: str) -> dict | None:
        with self.engine.connect() as conn:
            return db.get_refund(conn, refund_id)

    def list_pending(self) -> list[dict]:
        """Refunds waiting for a reviewer, oldest first (a queue)."""
        with self.engine.connect() as conn:
            return db.list_refunds_with_invoice(conn, [db.PENDING_APPROVAL], newest_first=False)

    def list_processed(self) -> list[dict]:
        with self.engine.connect() as conn:
            return db.list_refunds_with_invoice(conn, db.PROCESSED_STATUSES, newest_first=True)

    def metrics(self) -> dict[str, int]:
        with self.engine.connect() as conn:
            counts = db.count_refunds(conn)

        def total(status: str, processing_type: str | None = None) -> int:
            return sum(
                n for (s, p), n in counts.items() if s == status and (processing_type is None or p == processing_type)
            )

        return {
            "pending": total(db.PENDING_APPROVAL),
            "stp": total(db.REFUNDED, db.STP),
            "human_approved": total(db.REFUNDED, db.HUMAN_APPROVED),
            "rejected": total(db.REJECTED),
            "declined": total(db.DECLINED),
            "failed": total(db.FAILED),
        }
