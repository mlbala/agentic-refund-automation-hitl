"""Queued requests and the per-day batch run (Process button)."""

from datetime import timedelta

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
