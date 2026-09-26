"""Streamlit UI for the refund automation agent.

    uv run streamlit run app.py

The UI only talks to RefundService. Customer messages and LLM output are untrusted, so they
are shown with st.text (plain text), never rendered as Markdown.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import streamlit as st
from langchain.chat_models import init_chat_model

from refund_agent import db
from refund_agent.checkpointer import create_checkpointer, create_pool
from refund_agent.config import load_settings
from refund_agent.graph import build_graph
from refund_agent.service import RefundService

st.set_page_config(page_title="Refund Automation Agent", page_icon="💸", layout="wide")
logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("refund_agent").setLevel(logging.INFO)

# status -> (label, badge color, dot for tables)
STATUS_STYLES = {
    db.SUBMITTED: ("Submitted", "blue", "🔵"),
    db.PENDING_APPROVAL: ("Pending approval", "orange", "🟠"),
    db.DECIDING: ("Deciding", "blue", "🔵"),
    db.REFUNDED: ("Refunded", "green", "🟢"),
    db.REJECTED: ("Rejected", "red", "🔴"),
    db.DECLINED: ("Declined", "gray", "⚪"),
    db.FAILED: ("Failed", "violet", "🟣"),
}
PROCESSING_LABELS = {db.STP: "STP", db.HUMAN_APPROVED: "Human-approved"}


@st.cache_resource(show_spinner="Connecting to the database…")
def get_service() -> RefundService:
    """Engine, checkpoint pool, LLM and graph are created once per Streamlit server process."""
    settings = load_settings()
    engine = db.create_db_engine(settings.database_url)
    pool = create_pool(settings.database_url)
    checkpointer = create_checkpointer(pool)
    llm = init_chat_model(settings.llm_model)
    graph = build_graph(llm, checkpointer, engine, settings.approval_threshold)
    return RefundService(engine, graph, settings.approval_threshold)


# --- Formatting helpers ------------------------------------------------------------


def money(value) -> str:
    return "—" if value is None else f"${Decimal(value):,.2f}"


def when(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:  # SQLite returns naive datetimes; we always store UTC
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def status_badge(status: str) -> None:
    label, color, _ = STATUS_STYLES.get(status, (status, "gray", "⚪"))
    st.badge(label, color=color)


def status_text(status: str) -> str:
    label, _, dot = STATUS_STYLES.get(status, (status, "gray", "⚪"))
    return f"{dot} {label}"


def invoice_label(invoice: dict) -> str:
    return (
        f"{invoice['invoice_id']} · {invoice['customer_name']} · "
        f"{money(invoice['amount'])} · {invoice['payment_status']}"
    )


# --- Page sections -------------------------------------------------------------------


def render_sidebar(threshold: Decimal) -> str:
    with st.sidebar:
        st.header("Reviewer")
        reviewer = st.text_input(
            "Your name",
            key="reviewer",
            placeholder="e.g. Sam Lee",
            help="Required to approve or reject refunds. Saved as decided_by.",
        ).strip()
        if not reviewer:
            st.caption("Enter your name to approve or reject refunds.")
        st.divider()
        st.metric("Approval threshold", money(threshold))
        st.subheader("How it works")
        st.markdown(
            f"""
1. A refund request arrives for an invoice.
2. The AI agent looks up the invoice, decides the amount and calls the refund tool.
3. **{money(threshold)} or less** → refunded automatically (*straight-through processing*).
4. **Over {money(threshold)}** → the agent pauses and waits here for a human (*maker-checker*).
5. Approve or reject: the paused agent resumes and finishes the job.

The limits are enforced in code, so nothing in a customer message can bypass them.
"""
        )
        if st.button("Refresh", icon="🔄"):
            st.rerun()
    return reviewer


def render_flash() -> None:
    flash = st.session_state.pop("flash", None)
    if flash:
        kind, text = flash
        (st.success if kind == "success" else st.error)(text)


def render_metrics(service: RefundService) -> None:
    m = service.metrics()
    cols = st.columns(4)
    cols[0].metric("Pending approval", m["pending"])
    cols[1].metric("Auto-processed (STP)", m["stp"])
    cols[2].metric("Human-approved", m["human_approved"])
    cols[3].metric("Rejected / Declined", m["rejected"] + m["declined"])


def render_new_request(service: RefundService) -> None:
    st.subheader("New refund request")
    invoices = {inv["invoice_id"]: inv for inv in service.list_invoices()}
    with st.form("new_request", clear_on_submit=True):
        invoice_id = st.selectbox("Invoice", list(invoices), format_func=lambda i: invoice_label(invoices[i]))
        message = st.text_area(
            "Customer message",
            placeholder="The monitor arrived with dead pixels. I'd like a refund, please.",
        )
        submitted = st.form_submit_button("Submit to agent", type="primary")

    if submitted:
        if not message.strip():
            st.warning("Please enter the customer's message.")
        else:
            with st.spinner("Agent is processing…"):
                st.session_state["last_outcome"] = service.submit_refund(invoice_id, message)
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
        st.write(outcome["message"])
        if refund["agent_summary"]:
            st.caption("Agent summary")
            st.text(refund["agent_summary"])


def render_pending(service: RefundService, pending: list[dict], reviewer: str) -> None:
    if not pending:
        st.info(f"Nothing is waiting for approval. Refunds over {money(service.threshold)} will appear here.")
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
                st.metric("Requested refund", money(r["amount"]), help=f"Threshold: {money(service.threshold)}")
                st.caption("Customer message (untrusted)")
                st.text(r["customer_message"] or "—")
                st.caption("Agent reason")
                st.text(r["agent_reason"] or "—")

            note = st.text_input("Reviewer note", key=f"note_{rid}", placeholder="Why you approved or rejected")
            approve_col, reject_col, _ = st.columns([1, 1, 4])
            approve = approve_col.button("Approve", key=f"approve_{rid}", type="primary", disabled=not reviewer)
            reject = reject_col.button("Reject", key=f"reject_{rid}", disabled=not reviewer)
            if not reviewer:
                st.caption("Enter your name in the sidebar to approve or reject.")

            if approve or reject:
                with st.spinner("Resuming the agent…"):
                    out = service.decide_refund(rid, approved=approve, reviewer=reviewer, note=note)
                st.session_state["flash"] = ("success" if out["ok"] else "error", out["message"])
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
                "Decided by": r["decided_by"] or "—",
                "Note": r["reviewer_note"] or "",
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
            "Note": st.column_config.TextColumn(width="medium"),
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

    reviewer = render_sidebar(service.threshold)
    render_flash()
    render_metrics(service)
    st.divider()
    render_new_request(service)
    st.divider()

    pending = service.list_pending()
    pending_tab, processed_tab = st.tabs([f"Pending approval ({len(pending)})", "Processed"])
    with pending_tab:
        render_pending(service, pending, reviewer)
    with processed_tab:
        render_processed(service.list_processed())


main()
