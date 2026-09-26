"""Agent tools. Every business rule is enforced here in code, never left to the LLM.

issue_refund may pause the graph with interrupt(). On resume LangGraph runs the tool again
FROM THE TOP, so everything before interrupt() is read-only or idempotent, and real side
effects (money moving) happen only after the human decision.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool
from langgraph.types import interrupt
from sqlalchemy.engine import Engine

from . import db
from .config import to_money

logger = logging.getLogger(__name__)

# Statuses in which the refund is still open for the agent to act on.
OPEN_STATUSES = (db.SUBMITTED, db.PENDING_APPROVAL, db.DECIDING)


class _InvoiceNoLongerPaid(Exception):
    """Raised inside the processing transaction to roll it back."""


def _normalize_invoice_id(invoice_id: str) -> str:
    return (invoice_id or "").strip().upper()


def _invoice_for_llm(invoice: dict) -> dict:
    return {
        "invoice_id": invoice["invoice_id"],
        "customer_name": invoice["customer_name"],
        "customer_email": invoice["customer_email"],
        "item": invoice["item"],
        "quantity": invoice["quantity"],
        "amount": str(invoice["amount"]),
        "currency": invoice["currency"],
        "invoice_date": invoice["invoice_date"].isoformat(),
        "payment_status": invoice["payment_status"],
    }


def _utc_date(value: datetime) -> date:
    """Calendar date in UTC (SQLite returns naive datetimes, which we always store as UTC)."""
    return value.date() if value.tzinfo is None else value.astimezone(timezone.utc).date()


def _check_rules(
    refund: dict | None,
    invoice_id: str,
    invoice: dict | None,
    blocking: dict | None,
    amount: Decimal,
    refund_window_days: int | None = None,
) -> str | None:
    """Business rules and guardrails. Returns the reason for refusal, or None if the refund is allowed."""
    if refund is None:
        return "this refund request does not exist."
    if invoice_id != refund["invoice_id"]:  # rule 2
        return f"only invoice {refund['invoice_id']}, attached to this request, can be refunded."
    if invoice is None:  # rule 3
        return f"invoice {invoice_id} does not exist."
    if invoice["payment_status"] != db.PAID:
        return f"invoice {invoice_id} is '{invoice['payment_status']}', not 'paid'."
    # Guardrail: some items can never be refunded (gift cards, software licenses, final-sale clearance).
    if not invoice["refundable"]:
        return f"item '{invoice['item']}' on invoice {invoice_id} is non-refundable."
    # Guardrail: the person asking must be the invoice's customer (when the request says who asked).
    requester = (refund.get("requester_email") or "").strip().lower()
    if requester and requester != invoice["customer_email"].strip().lower():
        return f"the requester's email does not match the customer email on invoice {invoice_id}."
    # Guardrail: return window, measured when the refund was requested, so a slow approval
    # doesn't push a valid request out of the window.
    if refund_window_days is not None:
        age = (_utc_date(refund["created_at"]) - invoice["invoice_date"]).days
        if age > refund_window_days:
            return (
                f"invoice {invoice_id} is from {invoice['invoice_date']:%Y-%m-%d}, {age} days before the "
                f"request; refunds are only allowed within {refund_window_days} days."
            )
    if blocking is not None:  # rule 4
        return (
            f"invoice {invoice_id} already has refund {blocking['refund_id']} "
            f"({blocking['status']}). Only one refund per invoice is allowed."
        )
    if amount <= 0:  # rule 5
        return "the refund amount must be greater than $0.00."
    if amount > invoice["amount"]:
        return f"${amount} is more than the invoice amount ${invoice['amount']}."
    return None


def make_tools(engine: Engine, threshold: Decimal, refund_window_days: int | None = None) -> list[BaseTool]:
    """Build the agent's tools, bound to a database engine, the approval threshold and the
    return window (None = no window)."""

    @tool
    def get_invoice(invoice_id: str, config: RunnableConfig) -> dict:
        """Look up the invoice attached to this refund request (for example INV-1001). Read-only."""
        refund_id = config["configurable"]["thread_id"]
        invoice_id = _normalize_invoice_id(invoice_id)
        with engine.connect() as conn:
            refund = db.get_refund(conn, refund_id)
            invoice = db.get_invoice(conn, invoice_id)
        if refund is None:
            return {"error": "This refund request does not exist."}
        if invoice_id != refund["invoice_id"]:  # least privilege: no browsing other customers
            return {"error": f"Only invoice {refund['invoice_id']}, attached to this request, can be looked up."}
        if invoice is None:
            return {"error": f"Invoice {invoice_id} not found."}
        return _invoice_for_llm(invoice)

    @tool
    def issue_refund(invoice_id: str, amount: float, reason: str, config: RunnableConfig) -> str:
        """Refund the invoice attached to this request. Call it exactly once.

        Args:
            invoice_id: the invoice to refund, e.g. INV-1001.
            amount: refund amount in USD.
            reason: a short business reason for the refund.

        Large refunds are routed to a human reviewer automatically. The returned text is the
        authoritative outcome; only report a refund as complete if it says so.
        """
        # The refund ID comes from the graph's thread, never from the LLM.
        refund_id = config["configurable"]["thread_id"]
        invoice_id = _normalize_invoice_id(invoice_id)
        reason = (reason or "").strip()[:500]
        try:
            refund_amount = to_money(amount)
            if not refund_amount.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            return f"Refused: {amount!r} is not a valid amount. Nothing was changed."

        # 1. Validate (read-only). A refusal changes nothing.
        with engine.connect() as conn:
            refund = db.get_refund(conn, refund_id)
            invoice = db.get_invoice(conn, invoice_id)
            blocking = db.find_blocking_refund(conn, invoice_id, exclude_refund_id=refund_id)
        if refund is not None and refund["status"] not in OPEN_STATUSES:
            return f"Refund {refund_id} is already {refund['status']}. Nothing was changed."
        refusal = _check_rules(refund, invoice_id, invoice, blocking, refund_amount, refund_window_days)
        if refusal:
            logger.info("Refund %s refused: %s", refund_id, refusal)
            return f"Refused: {refusal} Nothing was changed."

        # 2 + 3a. Record the agent's decision. Compare-and-set on 'submitted', so the
        # re-run on resume (status is then 'deciding') writes nothing.
        needs_approval = refund_amount > threshold  # rule 1: strictly above; the threshold itself is STP
        with engine.begin() as conn:
            values = {"amount": refund_amount, "agent_reason": reason}
            if needs_approval:
                values["status"] = db.PENDING_APPROVAL
            db.update_refund(conn, refund_id, only_if_status=db.SUBMITTED, **values)

        if needs_approval:
            # 3b. Pause here. The checkpoint is saved and the graph stops until a reviewer
            # resumes it with Command(resume={"approved": bool, "reviewer": str, "note": str}).
            decision = interrupt(
                {
                    "type": "refund_approval",
                    "refund_id": refund_id,
                    "invoice_id": invoice_id,
                    "customer_name": invoice["customer_name"],
                    "invoice_amount": str(invoice["amount"]),
                    "refund_amount": str(refund_amount),
                    "threshold": str(threshold),
                    "agent_reason": reason,
                }
            )
            if not isinstance(decision, dict):
                decision = {}
            approved = decision.get("approved") is True  # anything else fails closed
            reviewer = str(decision.get("reviewer") or "").strip() or "unknown reviewer"
            note = str(decision.get("note") or "").strip()
            if not approved:
                with engine.begin() as conn:
                    changed = db.update_refund(
                        conn,
                        refund_id,
                        only_if_status=db.DECIDING,
                        status=db.REJECTED,
                        decided_by=reviewer,
                        reviewer_note=note or None,
                        decided_at=db.utcnow(),
                    )
                if not changed:
                    return f"Refund {refund_id} was already decided. Nothing was changed."
                return f"Declined by {reviewer}: {note or '(no reason given)'}"
            decided_by, processing_type, expected_status = reviewer, db.HUMAN_APPROVED, db.DECIDING
        else:
            # 4. Straight-through processing.
            decided_by, processing_type, expected_status, note = db.AI_AGENT, db.STP, db.SUBMITTED, ""

        # 5. Process in ONE transaction: the refund row and the invoice change together or not at all.
        now = db.utcnow()
        try:
            with engine.begin() as conn:
                processed = db.update_refund(
                    conn,
                    refund_id,
                    only_if_status=expected_status,
                    status=db.REFUNDED,
                    processing_type=processing_type,
                    decided_by=decided_by,
                    reviewer_note=note or None,
                    decided_at=now,
                    processed_at=now,
                )
                if not processed:
                    current = db.get_refund(conn, refund_id)["status"]
                    return f"Refund {refund_id} was already processed (status: {current}). Nothing was changed."
                if not db.mark_invoice_refunded(conn, invoice_id):
                    raise _InvoiceNoLongerPaid
        except _InvoiceNoLongerPaid:
            return f"Refused: invoice {invoice_id} is no longer 'paid'. Nothing was changed."

        # 6. Confirm.
        how = "automatically (STP)" if processing_type == db.STP else f"after approval by {decided_by}"
        logger.info("Refund %s processed %s", refund_id, how)
        return f"Refund {refund_id} completed: ${refund_amount} refunded for invoice {invoice_id} {how}."

    return [get_invoice, issue_refund]
