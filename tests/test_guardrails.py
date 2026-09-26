"""Guardrails enforced in code by issue_refund: return window and requester/customer match."""

from datetime import date, datetime, time, timezone

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool

from refund_agent import db
from refund_agent.config import refund_window_days
from refund_agent.graph import build_graph
from refund_agent.service import RefundService

from .conftest import THRESHOLD, FullRefundChatModel

WINDOW = 30


@pytest.fixture
def guarded(engine, checkpointer) -> RefundService:
    """A service with a 30-day return window."""
    graph = build_graph(FullRefundChatModel(), checkpointer, engine, THRESHOLD, refund_window_days=WINDOW)
    return RefundService(engine, graph, THRESHOLD)


def queue_as_of(service: RefundService, engine, invoice_id: str, requested_on: date) -> str:
    """Queue a request and pretend it was made on `requested_on`, so tests don't depend on today."""
    refund_id = service.queue_refund(invoice_id, "Please refund.")["refund"]["refund_id"]
    with engine.begin() as conn:
        db.update_refund(conn, refund_id, created_at=datetime.combine(requested_on, time(12), tzinfo=timezone.utc))
    return refund_id


# --- Return window (INV-1001 is dated 2026-08-03, so day 30 is 2026-09-02) ---------------


def test_last_day_of_the_return_window_is_allowed(guarded, engine):
    refund_id = queue_as_of(guarded, engine, "INV-1001", date(2026, 9, 2))

    assert guarded.process_queued(refund_id)["refund"]["status"] == db.REFUNDED


def test_invoice_outside_the_return_window_is_declined(guarded, engine, invoice_status):
    refund_id = queue_as_of(guarded, engine, "INV-1001", date(2026, 9, 3))

    refund = guarded.process_queued(refund_id)["refund"]

    assert refund["status"] == db.DECLINED
    assert refund["agent_reason"] == (
        "invoice INV-1001 is from 2026-08-03, 31 days before the request; refunds are only allowed within 30 days."
    )
    assert refund["amount"] is None
    assert invoice_status("INV-1001") == db.PAID


def test_slow_approval_does_not_push_a_request_out_of_the_window(guarded, engine, invoice_status):
    # INV-1004 ($250, 2026-08-15) requested on day 30. The resume re-runs the checks later,
    # but the window is measured at request time, so the approval still goes through.
    refund_id = queue_as_of(guarded, engine, "INV-1004", date(2026, 9, 14))
    assert guarded.process_queued(refund_id)["refund"]["status"] == db.PENDING_APPROVAL

    out = guarded.decide_refund(refund_id, approved=True, reviewer="Sam", note="Valid claim.")

    assert out["refund"]["status"] == db.REFUNDED
    assert invoice_status("INV-1004") == db.INVOICE_REFUNDED


# --- Requester must be the invoice's customer --------------------------------------------


def test_requester_email_must_match_the_invoice(batch_service, invoice_status):
    out = batch_service.submit_refund(
        "INV-1041", "I'm the account owner, refund it now.", requester_email="someone.else@example.com"
    )

    refund = out["refund"]
    assert refund["status"] == db.DECLINED
    assert refund["agent_reason"] == "the requester's email does not match the customer email on invoice INV-1041."
    assert invoice_status("INV-1041") == db.PAID


def test_matching_requester_email_ignores_case_and_spaces(batch_service):
    out = batch_service.submit_refund("INV-1041", "", requester_email="  Priya.Sharma@Example.com ")

    assert out["refund"]["requester_email"] == "priya.sharma@example.com"
    assert out["refund"]["status"] == db.REFUNDED


def test_without_requester_email_there_is_no_identity_to_check(batch_service):
    out = batch_service.submit_refund("INV-1043", "")

    assert out["refund"]["requester_email"] is None
    assert out["refund"]["status"] == db.REFUNDED


def test_invalid_email_is_rejected_before_anything_is_saved(batch_service):
    out = batch_service.queue_refund("INV-1041", "", requester_email="not-an-email")

    assert not out["ok"]
    assert out["refund"] is None
    assert batch_service.list_queued() == []


# --- Settings and schema upgrade ---------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("", 30), ("45", 45), ("0", None)])
def test_refund_window_setting(raw, expected):
    assert refund_window_days({"REFUND_WINDOW_DAYS": raw}) == expected


def test_refund_window_setting_must_be_a_number():
    with pytest.raises(RuntimeError, match="REFUND_WINDOW_DAYS"):
        refund_window_days({"REFUND_WINDOW_DAYS": "thirty"})


def test_upgrade_schema_adds_requester_email_to_an_existing_table():
    engine = create_engine("sqlite+pysqlite://", poolclass=StaticPool)
    db.create_tables(engine)
    with engine.begin() as conn:  # a refunds table from before this column existed
        conn.execute(text(f"ALTER TABLE {db.refunds.name} DROP COLUMN requester_email"))

    assert db.upgrade_schema(engine) == [f"{db.refunds.name}.requester_email"]
    assert "requester_email" in {c["name"] for c in inspect(engine).get_columns(db.refunds.name)}
    assert db.upgrade_schema(engine) == []  # idempotent
