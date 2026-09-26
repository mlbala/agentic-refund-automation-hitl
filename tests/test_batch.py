"""Queued requests and the per-day batch run (Process button)."""

from datetime import date, timedelta

import pytest

from refund_agent import db
from refund_agent.service import NO_CUSTOMER_MESSAGE, run_config


def test_queued_request_waits_without_running_the_agent(batch_service):
    out = batch_service.queue_refund("INV-1001", "The mouse stopped working.")

    refund = out["refund"]
    assert out["ok"]
    assert refund["status"] == db.QUEUED
    assert refund["amount"] is None
    assert batch_service.graph.get_state(run_config(refund["refund_id"])).values == {}  # agent never ran
    assert [r["refund_id"] for r in batch_service.list_queued()] == [refund["refund_id"]]
    assert batch_service.list_pending() == [] and batch_service.list_processed() == []
    assert batch_service.metrics()["queued"] == 1


def test_process_day_runs_the_agent_over_that_days_queue(batch_service, invoice_status):
    small = batch_service.queue_refund("INV-1001", "Mouse broken.")["refund"]["refund_id"]
    large = batch_service.queue_refund("INV-1004", "Dead pixels.")["refund"]["refund_id"]
    ineligible = batch_service.queue_refund("INV-1008", "Webcam refund.")["refund"]["refund_id"]
    progress = []

    results = batch_service.process_day(db.utcnow().date(), on_progress=lambda done, total, _: progress.append((done, total)))

    assert progress == [(1, 3), (2, 3), (3, 3)]
    assert [r["refund"]["refund_id"] for r in results] == [small, large, ineligible]  # oldest first
    assert batch_service.get_refund(small)["status"] == db.REFUNDED
    assert batch_service.get_refund(small)["processing_type"] == db.STP
    assert batch_service.get_refund(large)["status"] == db.PENDING_APPROVAL  # still needs a human
    assert batch_service.get_refund(ineligible)["status"] == db.DECLINED
    assert batch_service.get_refund(ineligible)["agent_reason"] == "invoice INV-1008 is 'refunded', not 'paid'."
    assert batch_service.list_queued() == []

    # The large one goes through the normal approval flow afterwards.
    out = batch_service.decide_refund(large, approved=True, reviewer="Sam", note="Photos confirm it.")
    assert out["refund"]["status"] == db.REFUNDED
    assert invoice_status("INV-1004") == db.INVOICE_REFUNDED


def test_process_day_only_touches_the_chosen_day(batch_service, engine):
    today = db.utcnow().date()
    yesterday = today - timedelta(days=1)
    old = batch_service.queue_refund("INV-1002", "Keyboard broken.")["refund"]["refund_id"]
    new = batch_service.queue_refund("INV-1006", "Hub broken.")["refund"]["refund_id"]
    with engine.begin() as conn:
        db.update_refund(conn, old, created_at=db.utcnow() - timedelta(days=1))

    results = batch_service.process_day(today)

    assert [r["refund"]["refund_id"] for r in results] == [new]
    assert batch_service.get_refund(old)["status"] == db.QUEUED
    assert [r["refund_id"] for r in batch_service.list_queued(yesterday)] == [old]

    batch_service.process_day(yesterday)

    assert batch_service.get_refund(old)["status"] == db.REFUNDED


def test_queued_request_is_processed_only_once(batch_service):
    refund_id = batch_service.queue_refund("INV-1001", "Mouse broken.")["refund"]["refund_id"]

    first = batch_service.process_queued(refund_id)
    second = batch_service.process_queued(refund_id)

    assert first["refund"]["status"] == db.REFUNDED
    assert not second["ok"]
    assert "already processed" in second["message"]
    assert batch_service.metrics()["stp"] == 1


def test_queue_all_invoices_from_a_date(batch_service):
    day = date(2026, 9, 25)  # INV-1039 … INV-1050; INV-1047 is already refunded
    dated = {inv["invoice_id"] for inv in batch_service.list_invoices(invoice_date=day)}
    already_queued = batch_service.queue_refund("INV-1043", "Backpack strap broke.")["refund"]["refund_id"]

    out = batch_service.queue_invoices_from(day)

    queued_invoices = {r["invoice_id"] for r in out["refunds"]}
    assert dated == {f"INV-{n}" for n in range(1039, 1051)}
    assert queued_invoices == dated - {"INV-1043", "INV-1047"}  # skips already requested + already refunded
    assert out["message"] == "Queued 10 invoice(s) from 2026-09-25; skipped 2 already refunded, already requested or non-refundable."
    assert batch_service.queue_invoices_from(day)["refunds"] == []  # a second click adds nothing

    results = batch_service.process_day(db.utcnow().date())

    by_status = {}
    for r in results:
        by_status.setdefault(r["refund"]["status"], set()).add(r["refund"]["invoice_id"])
    assert len(results) == 11 and already_queued in {r["refund"]["refund_id"] for r in results}
    assert by_status == {
        db.REFUNDED: {"INV-1039", "INV-1041", "INV-1043", "INV-1045", "INV-1048"},  # ≤ $99.99
        db.PENDING_APPROVAL: {"INV-1040", "INV-1042", "INV-1044", "INV-1046", "INV-1049", "INV-1050"},  # ≥ $100
    }


def test_invoice_view_shows_each_invoices_latest_refund(batch_service):
    day = date(2026, 9, 26)  # INV-1051 … INV-1060
    batch_service.submit_refund("INV-1052", "Tips don't fit.")  # $12.99 -> refunded
    batch_service.submit_refund("INV-1052", "Again please.")  # -> declined, and now the latest
    queued = batch_service.queue_refund("INV-1053", "")["refund"]["refund_id"]

    rows = {inv["invoice_id"]: inv for inv in batch_service.list_invoices_with_refunds(day)}

    assert set(rows) == {f"INV-{n}" for n in range(1051, 1061)}
    assert rows["INV-1052"]["latest_refund"]["status"] == db.DECLINED
    assert rows["INV-1052"]["payment_status"] == db.INVOICE_REFUNDED
    assert rows["INV-1053"]["latest_refund"] == {"invoice_id": "INV-1053", "refund_id": queued, "status": db.QUEUED}
    assert rows["INV-1051"]["latest_refund"] is None
    assert len(batch_service.list_invoices_with_refunds()) == 64  # no date = all invoices


@pytest.mark.parametrize("message", ["", "   ", None])
def test_customer_message_is_optional(batch_service, message):
    queued = batch_service.queue_refund("INV-1001", message)["refund"]
    processed_now = batch_service.submit_refund("INV-1006", message)["refund"]

    assert queued["customer_message"] == NO_CUSTOMER_MESSAGE
    assert processed_now["customer_message"] == NO_CUSTOMER_MESSAGE
    assert processed_now["status"] == db.REFUNDED
    assert batch_service.process_queued(queued["refund_id"])["refund"]["status"] == db.REFUNDED


def test_process_now_skips_the_queue(batch_service):
    out = batch_service.submit_refund("INV-1001", "Mouse broken.")

    assert out["refund"]["status"] == db.REFUNDED
    assert batch_service.list_queued() == []
