"""Streamlit UI for the refund automation agent.

    uv run streamlit run app.py

The UI only talks to RefundService. Customer messages and LLM output are untrusted, so they
are shown with st.text (plain text), never rendered as Markdown.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pandas as pd
import streamlit as st
from langchain.chat_models import init_chat_model

from refund_agent import db
from refund_agent.checkpointer import create_checkpointer, create_pool
from refund_agent.config import CENTS, load_settings
from refund_agent.graph import build_graph
from refund_agent.service import RefundService

st.set_page_config(page_title="Refund Automation Agent", page_icon="💸", layout="wide")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("refund_agent").setLevel(logging.INFO)

# status -> (label, badge color, dot for tables)
STATUS_STYLES = {
    db.QUEUED: ("Queued", "gray", "⏳"),
    db.SUBMITTED: ("Submitted", "blue", "🔵"),
    db.PENDING_APPROVAL: ("Pending approval", "orange", "🟠"),
    db.DECIDING: ("Deciding", "blue", "🔵"),
    db.REFUNDED: ("Refunded", "green", "🟢"),
    db.REJECTED: ("Declined (reviewer)", "red", "🔴"),
    db.DECLINED: ("Declined (agent)", "gray", "⚪"),
    db.FAILED: ("Failed", "violet", "🟣"),
}
PROCESSING_LABELS = {db.STP: "STP", db.HUMAN_APPROVED: "Human-approved"}

# Tab keys; the app switches to the tab where the last action's result shows up.
QUEUE_TAB, PENDING_TAB, PROCESSED_TAB, INVOICES_TAB = "queue", "pending", "processed", "invoices"


@st.cache_resource(show_spinner="Connecting to the database…")
def get_service() -> RefundService:
    """Engine, checkpoint pool, LLM and graph are created once per Streamlit server process."""
    settings = load_settings()
    engine = db.create_db_engine(settings.database_url)
    pool = create_pool(settings.database_url)
    checkpointer = create_checkpointer(pool)
    llm = init_chat_model(settings.llm_model)
    graph = build_graph(llm, checkpointer, engine, settings.approval_threshold, settings.refund_window_days)
    return RefundService(engine, graph, settings.approval_threshold, settings.refund_window_days)


# --- Formatting helpers ------------------------------------------------------------


def money(value) -> str:
    return "—" if value is None else f"${Decimal(value):,.2f}"


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:  # SQLite returns naive datetimes; we always store UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def when(value: datetime | None) -> str:
    return "—" if value is None else as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def status_badge(status: str) -> None:
    label, color, _ = STATUS_STYLES.get(status, (status, "gray", "⚪"))
    st.badge(label, color=color)


def status_text(status: str) -> str:
    label, _, dot = STATUS_STYLES.get(status, (status, "gray", "⚪"))
    return f"{dot} {label}"


def approval_starts_at(threshold: Decimal) -> str:
    """Smallest amount that needs approval (amounts are whole cents), e.g. 99.99 -> $100.00."""
    return money(threshold + CENTS)


def invoice_label(invoice: dict) -> str:
    return (
        f"{invoice['invoice_id']} · {invoice['customer_name']} · "
        f"{money(invoice['amount'])} · {invoice['payment_status']} · {invoice['invoice_date']:%Y-%m-%d}"
    )


def decision_reason(refund: dict) -> str:
    """Why a refund ended the way it did: the reviewer's reason for human decisions, else the agent's."""
    if refund["status"] == db.REJECTED or refund["processing_type"] == db.HUMAN_APPROVED:
        return refund["reviewer_note"] or ""
    if refund["status"] == db.FAILED:
        return refund["agent_summary"] or ""
    return refund["agent_reason"] or ""


def tab_for(refund: dict | None) -> str:
    """The tab where this refund now shows up."""
    if refund is None or refund["status"] == db.QUEUED:
        return QUEUE_TAB
    if refund["status"] == db.PENDING_APPROVAL:
        return PENDING_TAB
    return PROCESSED_TAB


def flash(ok: bool, message: str) -> None:
    st.session_state["flash"] = ("success" if ok else "error", message)


# --- Page sections -------------------------------------------------------------------


def render_sidebar(threshold: Decimal, refund_window_days: int | None) -> str:
    with st.sidebar:
        st.header("Reviewer")
        reviewer = st.text_input(
            "Your name",
            key="reviewer",
            placeholder="e.g. Sam Lee",
            help="Required to approve or decline refunds. Saved as decided_by.",
        ).strip()
        if not reviewer:
            st.caption("Enter your name to approve or decline refunds.")
        st.divider()
        threshold_col, window_col = st.columns(2)
        threshold_col.metric("Approval threshold", money(threshold))
        window_col.metric("Return window", f"{refund_window_days} days" if refund_window_days else "None")
        st.subheader("How it works")
        st.markdown(
            f"""
1. A refund request arrives for an invoice. **Process now**, or **Add to queue** and
   process a whole day's queue at once from the *Queue* tab.
2. The AI agent looks up the invoice, decides the amount and calls the refund tool.
3. **{money(threshold)} or less** → refunded automatically (*straight-through processing*).
4. **{approval_starts_at(threshold)} or more** → the agent pauses and waits for a human (*maker-checker*).
5. **Approve** or **Decline** with a reason: the paused agent resumes and finishes the job.

**Guardrails**, enforced in code, so nothing in a customer message can bypass them:
the invoice must be paid and not already refunded, one refund per invoice, amount ≤ invoice,
{f"requested within {refund_window_days} days of the invoice date" if refund_window_days else "no return window"},
and a customer email, when given, must match the invoice.
"""
        )
        if st.button("Refresh", icon="🔄"):
            st.rerun()
    return reviewer


def render_flash() -> None:
    flash_message = st.session_state.pop("flash", None)
    if flash_message:
        kind, text = flash_message
        (st.success if kind == "success" else st.error)(text)


def render_metrics(service: RefundService) -> None:
    m = service.metrics()
    cols = st.columns(5)
    cols[0].metric("Queued", m["queued"])
    cols[1].metric("Pending approval", m["pending"])
    cols[2].metric("Auto-processed (STP)", m["stp"])
    cols[3].metric("Human-approved", m["human_approved"])
    cols[4].metric("Declined (reviewer / agent)", m["rejected"] + m["declined"])


def clear_date(key: str) -> None:
    st.session_state[key] = None


def pick_invoice_date(all_invoices: list[dict], key: str, help: str) -> tuple[date | None, list[dict]]:
    """Calendar for an invoice date (empty = all dates) with an "All dates" reset.

    Returns (the chosen date or None, the invoices shown for it).
    """
    dates = [inv["invoice_date"] for inv in all_invoices]
    date_col, info_col, all_col = st.columns([1, 2, 1], vertical_alignment="bottom")
    chosen = date_col.date_input(
        "Invoice date",
        value=None,
        min_value=min(dates, default=None),
        max_value=max([*dates, utc_today()]),
        format="YYYY-MM-DD",
        key=key,
        help=help,
    )
    if chosen is None:
        info_col.caption(f"All dates: {len(all_invoices)} invoices. Pick a date to filter.")
        return None, all_invoices

    all_col.button("All dates", icon="✖️", key=f"{key}_clear", on_click=clear_date, args=(key,))
    day_invoices = [inv for inv in all_invoices if inv["invoice_date"] == chosen]
    info_col.caption(
        f"{len(day_invoices)} invoice(s) on {chosen:%Y-%m-%d}." if day_invoices else f"No invoices dated {chosen:%Y-%m-%d}."
    )
    return chosen, day_invoices


def render_new_request(service: RefundService) -> None:
    st.subheader("New refund request")
    _, shown = pick_invoice_date(
        service.list_invoices(),
        key="request_date",
        help="Pick a date to list only that day's invoices in the Invoice dropdown. Leave empty for all dates.",
    )
    invoices = {inv["invoice_id"]: inv for inv in shown}
    if not invoices:
        st.info("Pick another date, or click **All dates** to see every invoice.")
        return
    with st.form("new_request", clear_on_submit=True):
        invoice_id = st.selectbox("Invoice", list(invoices), format_func=lambda i: invoice_label(invoices[i]))
        requester_email = st.text_input(
            "Customer email (optional)",
            placeholder="e.g. alice.martin@example.com",
            help="Who is asking. If given, it must match the invoice's customer email, or the agent declines.",
        )
        message = st.text_area(
            "Customer message (optional)",
            placeholder="e.g. The monitor arrived with dead pixels. I'd like a refund, please.",
            help="Context for the agent and the reviewer. Without a message the agent refunds the full amount.",
        )
        now_col, queue_col, _ = st.columns([1, 1, 4])
        process_now = now_col.form_submit_button("Process now", icon="⚡", type="primary")
        add_to_queue = queue_col.form_submit_button("Add to queue", icon="📥")

    if process_now or add_to_queue:
        if process_now:
            with st.spinner("Agent is processing…"):
                outcome = service.submit_refund(invoice_id, message, requester_email)
        else:
            outcome = service.queue_refund(invoice_id, message, requester_email)
        st.session_state["last_outcome"] = outcome
        st.session_state["active_tab"] = tab_for(outcome["refund"])
        st.rerun()  # refresh metrics and tabs with the new state

    outcome = st.session_state.get("last_outcome")
    if outcome:
        render_outcome(outcome)


def render_outcome(outcome: dict) -> None:
    refund = outcome["refund"]
    with st.container(border=True):
        if refund is None:
            st.error(outcome["message"])
            return
        status_badge(refund["status"])
        st.markdown(f"**{refund['refund_id']}** · {refund['invoice_id']} · {money(refund['amount'])}")
        st.text(outcome["message"])
        if refund["agent_summary"]:
            st.caption("Agent summary")
            st.text(refund["agent_summary"])


def render_queue(service: RefundService) -> None:
    left, right = st.columns([1, 3])
    day = left.date_input("Day (UTC)", value=utc_today(), key="batch_day")
    queued = service.list_queued(day)

    other_days: dict[date, int] = {}
    for r in service.list_queued():
        created = as_utc(r["created_at"]).date()
        if created != day:
            other_days[created] = other_days.get(created, 0) + 1
    if other_days:
        right.caption(
            "Also queued on: " + ", ".join(f"{d:%Y-%m-%d} ({n})" for d, n in sorted(other_days.items()))
        )

    results = st.session_state.pop("batch_results", None)
    if results:
        st.markdown("**Last batch run**")
        st.dataframe(pd.DataFrame(results), hide_index=True)

    if not queued:
        st.info(f"No requests queued for {day:%Y-%m-%d}. Use **Add to queue** above to queue one.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Refund ID": r["refund_id"],
                    "Invoice": r["invoice_id"],
                    "Customer": r["customer_name"],
                    "Invoice amount": money(r["invoice_amount"]),
                    "Customer message": r["customer_message"],
                    "Queued at": when(r["created_at"]),
                }
                for r in queued
            ]
        ),
        hide_index=True,
    )
    label = f"Process {len(queued)} request{'s' if len(queued) != 1 else ''} for {day:%Y-%m-%d}"
    if st.button(label, icon="▶️", type="primary"):
        progress = st.progress(0.0, text="Starting…")
        log = st.container()
        rows = []

        def on_progress(done: int, total: int, result: dict) -> None:
            refund = result["refund"] or {}
            rows.append(
                {
                    "Refund ID": refund.get("refund_id", "—"),
                    "Invoice": refund.get("invoice_id", "—"),
                    "Amount": money(refund.get("amount")),
                    "Outcome": status_text(refund.get("status", "")),
                    "Details": result["message"],
                }
            )
            log.text(f"{rows[-1]['Refund ID']} · {rows[-1]['Invoice']} → {rows[-1]['Outcome']}")
            progress.progress(done / total, text=f"Processed {done} of {total}")

        with st.spinner("Agent is processing the queue…"):
            service.process_day(day, on_progress=on_progress)
        st.session_state["batch_results"] = rows
        waiting = sum(1 for row in rows if row["Outcome"] == status_text(db.PENDING_APPROVAL))
        flash(True, f"Processed {len(rows)} queued request(s) for {day:%Y-%m-%d}; {waiting} need approval.")
        st.session_state["active_tab"] = QUEUE_TAB
        st.rerun()


def render_pending(service: RefundService, pending: list[dict], reviewer: str) -> None:
    if not pending:
        st.info(
            f"Nothing is waiting for approval. Refunds of {approval_starts_at(service.threshold)} "
            "or more will appear here."
        )
        return

    for i, r in enumerate(pending):
        rid = r["refund_id"]
        title = f"{rid} · {r['invoice_id']} · {r['customer_name']} · {money(r['amount'])}"
        with st.expander(title, expanded=i == 0):
            st.caption(f"Created {when(r['created_at'])}")
            invoice_col, request_col = st.columns(2)
            with invoice_col:
                st.markdown("**Invoice**")
                st.markdown(
                    f"""
- **Customer:** {r['customer_name']} ({r['customer_email']})
- **Item:** {r['item']} × {r['quantity']}
- **Invoice amount:** {money(r['invoice_amount'])} {r['currency']}
- **Invoice date:** {r['invoice_date']:%Y-%m-%d}
- **Payment status:** {r['payment_status']}
"""
                )
            with request_col:
                st.metric(
                    "Requested refund",
                    money(r["amount"]),
                    help=f"Approval needed from {approval_starts_at(service.threshold)}",
                )
                st.caption("Requested by")
                st.text(r["requester_email"] or "Staff (no customer email given)")
                st.caption("Customer message (untrusted)")
                st.text(r["customer_message"] or "—")
                st.caption("Agent reason")
                st.text(r["agent_reason"] or "—")

            # A form submits the reason together with the button click.
            with st.form(f"decision_{rid}"):
                reason = st.text_area(
                    "Reason (required)",
                    key=f"reason_{rid}",
                    placeholder="Why you approve or decline this refund",
                    height=80,
                )
                approve_col, decline_col, _ = st.columns([1, 1, 4])
                approve = approve_col.form_submit_button("Approve", icon="✅", type="primary", disabled=not reviewer)
                decline = decline_col.form_submit_button("Decline", icon="❌", disabled=not reviewer)
            if not reviewer:
                st.caption("Enter your name in the sidebar to approve or decline.")

            if approve or decline:
                if not reason.strip():
                    st.error("Please enter a reason before approving or declining.")
                else:
                    with st.spinner("Resuming the agent…"):
                        out = service.decide_refund(rid, approved=approve, reviewer=reviewer, note=reason)
                    flash(out["ok"], out["message"])
                    st.session_state["active_tab"] = tab_for(out["refund"]) if out["ok"] else PENDING_TAB
                    st.rerun()


def render_invoices(service: RefundService) -> None:
    """Browse invoices by date, with each invoice's latest refund, and queue a whole day at once."""
    chosen, shown = pick_invoice_date(
        service.list_invoices_with_refunds(),
        key="invoices_view_date",
        help="Pick a date to see that day's invoices. Leave empty for all dates.",
    )
    if not shown:
        st.info("Pick another date, or click **All dates** to see every invoice.")
        return

    def refund_cell(latest: dict | None) -> str:
        return "—" if latest is None else f"{status_text(latest['status'])} · {latest['refund_id']}"

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Invoice": inv["invoice_id"],
                    "Date": f"{inv['invoice_date']:%Y-%m-%d}",
                    "Customer": inv["customer_name"],
                    "Email": inv["customer_email"],
                    "Item": inv["item"],
                    "Qty": inv["quantity"],
                    "Amount": money(inv["amount"]),
                    "Payment status": inv["payment_status"],
                    "Latest refund": refund_cell(inv["latest_refund"]),
                }
                for inv in shown
            ]
        ),
        hide_index=True,
    )
    if chosen is None:
        st.caption("Pick a date to queue all of that day's invoices at once.")
        return
    has_paid = any(inv["payment_status"] == db.PAID for inv in shown)
    if st.button(
        f"Queue all invoices from {chosen:%Y-%m-%d}",
        icon="📥",
        disabled=not has_paid,
        help="Queues a refund request for each paid invoice from this date. Already refunded or "
        "already requested invoices are skipped. Then use ▶ Process in the Queue tab.",
    ):
        out = service.queue_invoices_from(chosen)
        flash(out["ok"], out["message"])
        st.session_state.pop("last_outcome", None)
        st.session_state["active_tab"] = QUEUE_TAB
        st.rerun()


def render_processed(processed: list[dict]) -> None:
    if not processed:
        st.info("No processed refunds yet.")
        return
    table = pd.DataFrame(
        [
            {
                "Refund ID": r["refund_id"],
                "Invoice": r["invoice_id"],
                "Customer": r["customer_name"],
                "Amount": money(r["amount"]),
                "Status": status_text(r["status"]),
                "Processing": PROCESSING_LABELS.get(r["processing_type"], "—"),
                "Requested by": r["requester_email"] or "staff",
                "Decided by": r["decided_by"] or "—",
                "Reason": decision_reason(r),
                "Decided at": when(r["decided_at"]),
                "Agent summary": r["agent_summary"] or "",
            }
            for r in processed
        ]
    )
    st.dataframe(
        table,
        hide_index=True,
        column_config={
            "Reason": st.column_config.TextColumn(width="large"),
            "Agent summary": st.column_config.TextColumn(width="large"),
        },
    )


# --- Page ----------------------------------------------------------------------------


def main() -> None:
    st.title("Refund Automation Agent")
    st.caption("LangGraph ReAct agent with human-in-the-loop approval · maker-checker for large refunds")

    try:
        service = get_service()
    except Exception as exc:  # e.g. missing .env; the message never includes the connection string
        st.error(f"Could not start the app: {type(exc).__name__}: {exc}")
        st.stop()

    reviewer = render_sidebar(service.threshold, service.refund_window_days)
    render_flash()
    render_metrics(service)
    st.divider()
    render_new_request(service)
    st.divider()

    queued_count = len(service.list_queued())
    pending = service.list_pending()
    labels = {
        QUEUE_TAB: f"Queue ({queued_count})",
        PENDING_TAB: f"Pending approval ({len(pending)})",
        PROCESSED_TAB: "Processed",
        INVOICES_TAB: "Invoices",
    }
    active = st.session_state.get("active_tab", PENDING_TAB)
    queue_tab, pending_tab, processed_tab, invoices_tab = st.tabs(list(labels.values()), default=labels[active])
    with queue_tab:
        render_queue(service)
    with pending_tab:
        render_pending(service, pending, reviewer)
    with processed_tab:
        render_processed(service.list_processed())
    with invoices_tab:
        render_invoices(service)


main()
