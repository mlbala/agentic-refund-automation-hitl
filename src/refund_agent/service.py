"""Service layer: the only API the UI calls.

The database is the source of truth for a refund's status. After every agent run we re-read
the refund row; we never parse the LLM's text to find out what happened.

Two ways to get a request processed:
- submit_refund(): create the request and run the agent right away.
- queue_refund() during the day, then process_day(): run the agent over one day's queue.

Every action returns {"ok": bool, "message": str, "refund": dict | None}.
"""

import json
import logging
import secrets
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command
from sqlalchemy.engine import Engine

from . import db

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 12
# Stored when a request comes without a customer message; the agent then refunds the full amount.
NO_CUSTOMER_MESSAGE = "(no customer message)"


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


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """Start and end of a calendar day in UTC, the timezone all timestamps are stored in."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)
    return start, start + timedelta(days=1)


def _result(ok: bool, message: str, refund: dict | None = None) -> dict:
    return {"ok": ok, "message": message, "refund": refund}


def _error_text(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _final_ai_text(messages: list) -> str | None:
    """The agent's closing reply: the last message, if it is an AI message with text."""
    if messages and isinstance(messages[-1], AIMessage):
        return messages[-1].text.strip() or None
    return None


def _refusal_reason(messages: list) -> str:
    """Why a run ended without a refund, taken from the tools' own output (our code, not the LLM)."""
    for message in reversed(messages):
        if not isinstance(message, ToolMessage):
            continue
        text = message.text.strip()
        if text.startswith("Refused: "):
            return text.removeprefix("Refused: ").removesuffix(" Nothing was changed.")
        try:
            error = json.loads(text).get("error")  # get_invoice returns {"error": ...}
        except (ValueError, AttributeError):
            error = None
        if error:
            return error
    return "The agent ended without issuing a refund."


class RefundService:
    def __init__(self, engine: Engine, graph: CompiledStateGraph, threshold: Decimal):
        self.engine = engine
        self.graph = graph
        self.threshold = threshold

    # --- Actions -------------------------------------------------------------

    def submit_refund(self, invoice_id: str, customer_message: str) -> dict:
        """Create a refund request and run the agent on it right away."""
        created = self._create_refund(invoice_id, customer_message, db.SUBMITTED)
        if not created["ok"]:
            return created
        return self._run_agent(created["refund"])

    def queue_refund(self, invoice_id: str, customer_message: str) -> dict:
        """Save a refund request for the batch run (process_day). The agent doesn't see it yet."""
        created = self._create_refund(invoice_id, customer_message, db.QUEUED)
        if created["ok"]:
            created["message"] = self.describe(created["refund"])
        return created

    def queue_invoices_from(self, invoice_date: date, customer_message: str = "") -> dict:
        """Queue a refund request for every invoice dated `invoice_date` that still needs one.

        Skips invoices that are not 'paid' or already have a refund queued, in progress or done,
        so clicking twice doesn't create duplicates. Returns {"ok", "message", "refunds"}.
        """
        with self.engine.connect() as conn:
            invoices = db.list_invoices(conn, invoice_date=invoice_date)
            taken = db.invoice_ids_with_refunds(conn, db.ACTIVE_STATUSES)
        eligible = [inv for inv in invoices if inv["payment_status"] == db.PAID and inv["invoice_id"] not in taken]
        queued = [self.queue_refund(inv["invoice_id"], customer_message)["refund"] for inv in eligible]
        message = f"Queued {len(queued)} invoice(s) from {invoice_date:%Y-%m-%d}"
        if skipped := len(invoices) - len(queued):
            message += f"; skipped {skipped} already refunded or already requested"
        return {"ok": True, "message": message + ".", "refunds": queued}

    def process_queued(self, refund_id: str) -> dict:
        """Run the agent on one queued request."""
        # Claim: queued -> submitted. Atomic, so two batch runs never process the same request.
        with self.engine.begin() as conn:
            claimed = db.update_refund(conn, refund_id, only_if_status=db.QUEUED, status=db.SUBMITTED)
            refund = db.get_refund(conn, refund_id)
        if refund is None:
            return _result(False, f"Refund {refund_id} not found.")
        if not claimed:
            return _result(False, f"Refund {refund_id} was already processed (status: {refund['status']}).", refund)
        return self._run_agent(refund)

    def process_day(
        self, day: date, on_progress: Callable[[int, int, dict], None] | None = None
    ) -> list[dict]:
        """Run the agent over every request queued on `day` (UTC), oldest first.

        on_progress(done, total, result) is called after each request, e.g. to update a progress bar.
        """
        queued = self.list_queued(day)
        results = []
        for done, refund in enumerate(queued, start=1):
            result = self.process_queued(refund["refund_id"])
            results.append(result)
            if on_progress:
                on_progress(done, len(queued), result)
        logger.info("Batch for %s: processed %d request(s)", day, len(results))
        return results

    def decide_refund(self, refund_id: str, approved: bool, reviewer: str, note: str) -> dict:
        """A reviewer's decision on a pending refund. Both a reviewer name and a reason are required."""
        reviewer = (reviewer or "").strip()
        note = (note or "").strip()
        if not reviewer:
            return _result(False, "Reviewer name is required to approve or decline.")
        if not note:
            return _result(False, "A reason is required to approve or decline.")

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
        logger.info("Refund %s: %s by reviewer", refund_id, "approved" if approved else "declined")
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

    # --- Internals -------------------------------------------------------------

    def _create_refund(self, invoice_id: str, customer_message: str, status: str) -> dict:
        customer_message = (customer_message or "").strip() or NO_CUSTOMER_MESSAGE
        refund_id = new_refund_id()
        with self.engine.begin() as conn:
            if db.get_invoice(conn, invoice_id) is None:
                return _result(False, f"Invoice {invoice_id} not found.")
            db.insert_refund(conn, refund_id, invoice_id, customer_message, status=status)
            refund = db.get_refund(conn, refund_id)
        logger.info("Refund %s created for %s (%s)", refund_id, invoice_id, status)
        return _result(True, f"Refund {refund_id} created.", refund)

    def _run_agent(self, refund: dict) -> dict:
        """Run the agent on a refund in status 'submitted', then reconcile the row."""
        refund_id = refund["refund_id"]
        first_message = HumanMessage(request_message(refund_id, refund["invoice_id"], refund["customer_message"]))
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

    def _after_run(self, refund_id: str, output: dict) -> dict:
        """Reconcile the refund row after a graph run that finished or paused."""
        if output.get("__interrupt__"):
            with self.engine.connect() as conn:
                refund = db.get_refund(conn, refund_id)
            return _result(True, self.describe(refund), refund)

        messages = output.get("messages", [])
        with self.engine.begin() as conn:
            db.update_refund(conn, refund_id, agent_summary=_final_ai_text(messages))
            refund = db.get_refund(conn, refund_id)
            if refund["status"] in (db.SUBMITTED, db.DECIDING):
                # The run ended but issue_refund never moved the refund forward: the agent declined.
                values = {"status": db.DECLINED, "decided_by": db.AI_AGENT, "decided_at": db.utcnow()}
                if refund["agent_reason"] is None:
                    values["agent_reason"] = _refusal_reason(messages)
                db.update_refund(conn, refund_id, only_if_status=[db.SUBMITTED, db.DECIDING], **values)
                refund = db.get_refund(conn, refund_id)
        return _result(refund["status"] != db.FAILED, self.describe(refund), refund)

    def describe(self, refund: dict) -> str:
        status, amount = refund["status"], refund["amount"]
        if status == db.QUEUED:
            return f"Queued {refund['refund_id']} for {refund['invoice_id']}; it will be processed in the batch run."
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
            return f"Declined by {refund['decided_by']}: {refund['reviewer_note']}"
        if status == db.DECLINED:
            return f"Declined by the agent: {refund['agent_reason'] or 'the request is not eligible.'}"
        return f"Status: {status}."

    # --- Reads for the UI ------------------------------------------------------

    def list_invoices(self, invoice_date: date | None = None) -> list[dict]:
        with self.engine.connect() as conn:
            return db.list_invoices(conn, invoice_date=invoice_date)

    def get_refund(self, refund_id: str) -> dict | None:
        with self.engine.connect() as conn:
            return db.get_refund(conn, refund_id)

    def list_queued(self, day: date | None = None) -> list[dict]:
        """Requests waiting for the batch run, oldest first; only those created on `day` (UTC) if given."""
        created_from, created_before = day_bounds(day) if day else (None, None)
        with self.engine.connect() as conn:
            return db.list_refunds_with_invoice(
                conn, [db.QUEUED], newest_first=False, created_from=created_from, created_before=created_before
            )

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
            "queued": total(db.QUEUED),
            "pending": total(db.PENDING_APPROVAL),
            "stp": total(db.REFUNDED, db.STP),
            "human_approved": total(db.REFUNDED, db.HUMAN_APPROVED),
            "rejected": total(db.REJECTED),
            "declined": total(db.DECLINED),
            "failed": total(db.FAILED),
        }
