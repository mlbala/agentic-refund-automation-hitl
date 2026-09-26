"""App tables (SQLAlchemy Core), seed data and small query helpers.

SQL stays portable (no Postgres-only features) so the tests can run on SQLite.
Helpers take a Connection so callers decide the transaction boundary:
    with engine.begin() as conn:   # one transaction
        ...
"""

from collections.abc import Iterable
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    create_engine,
    delete,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine

from .config import sqlalchemy_url

# Refund statuses: submitted -> pending_approval -> deciding -> refunded | rejected,
# plus declined (agent found it ineligible) and failed (error).
SUBMITTED = "submitted"
PENDING_APPROVAL = "pending_approval"
DECIDING = "deciding"
REFUNDED = "refunded"
REJECTED = "rejected"
DECLINED = "declined"
FAILED = "failed"

PROCESSED_STATUSES = (REFUNDED, REJECTED, DECLINED, FAILED)
# While a refund is in one of these states, no other refund may target the same invoice.
BLOCKING_STATUSES = (PENDING_APPROVAL, DECIDING, REFUNDED)

# Processing types and the decided_by value for automatic decisions.
STP = "stp"
HUMAN_APPROVED = "human_approved"
AI_AGENT = "ai-agent"

# Invoice payment statuses.
PAID = "paid"
INVOICE_REFUNDED = "refunded"

metadata = MetaData()

invoices = Table(
    "invoices",
    metadata,
    Column("invoice_id", Text, primary_key=True),
    Column("customer_name", Text, nullable=False),
    Column("customer_email", Text, nullable=False),
    Column("item", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("amount", Numeric(10, 2), nullable=False),
    Column("currency", Text, nullable=False, default="USD", server_default="USD"),
    Column("invoice_date", Date, nullable=False),
    Column("payment_status", Text, nullable=False),
)

refunds = Table(
    "refunds",
    metadata,
    Column("refund_id", Text, primary_key=True),  # also the LangGraph thread_id
    Column("invoice_id", Text, ForeignKey("invoices.invoice_id"), nullable=False),
    Column("customer_message", Text, nullable=False),
    Column("amount", Numeric(10, 2), nullable=True),  # set when the agent decides
    Column("status", Text, nullable=False),
    Column("processing_type", Text, nullable=True),
    Column("agent_reason", Text, nullable=True),
    Column("agent_summary", Text, nullable=True),
    Column("decided_by", Text, nullable=True),
    Column("reviewer_note", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("processed_at", DateTime(timezone=True), nullable=True),
)


def _invoice(invoice_id, name, email, item, quantity, amount, invoice_date, status=PAID) -> dict:
    return {
        "invoice_id": invoice_id,
        "customer_name": name,
        "customer_email": email,
        "item": item,
        "quantity": quantity,
        "amount": Decimal(amount),
        "currency": "USD",
        "invoice_date": invoice_date,
        "payment_status": status,
    }


SEED_INVOICES = [
    _invoice("INV-1001", "Alice Martin", "alice.martin@example.com", "Wireless mouse", 1, "25.00", date(2026, 8, 3)),
    _invoice("INV-1002", "Ben Carter", "ben.carter@example.com", "Mechanical keyboard", 1, "80.00", date(2026, 8, 7)),
    _invoice("INV-1003", "Chloe Nguyen", "chloe.nguyen@example.com", "Headphones", 1, "100.00", date(2026, 8, 12)),
    _invoice("INV-1004", "Dev Patel", "dev.patel@example.com", '27" monitor', 1, "250.00", date(2026, 8, 15)),
    _invoice("INV-1005", "Emma Rossi", "emma.rossi@example.com", "Office chair", 1, "420.00", date(2026, 8, 20)),
    _invoice("INV-1006", "Farid Khan", "farid.khan@example.com", "USB-C hub", 1, "45.00", date(2026, 8, 24)),
    _invoice("INV-1007", "Grace Lee", "grace.lee@example.com", "Laptop stand", 2, "150.00", date(2026, 9, 1)),
    _invoice("INV-1008", "Hiro Tanaka", "hiro.tanaka@example.com", "Webcam", 1, "60.00", date(2026, 9, 5), INVOICE_REFUNDED),
]


# --- Setup -----------------------------------------------------------------


def create_db_engine(database_url: str) -> Engine:
    return create_engine(sqlalchemy_url(database_url), pool_pre_ping=True)


def create_tables(engine: Engine) -> None:
    metadata.create_all(engine)


def seed_invoices(engine: Engine) -> int:
    """Insert seed invoices that don't exist yet. Returns how many were added."""
    with engine.begin() as conn:
        existing = set(conn.scalars(select(invoices.c.invoice_id)))
        new_rows = [row for row in SEED_INVOICES if row["invoice_id"] not in existing]
        if new_rows:
            conn.execute(insert(invoices), new_rows)
    return len(new_rows)


def reset_demo_data(engine: Engine) -> list[str]:
    """Delete all refunds and restore seed invoice statuses. Returns the deleted refund IDs."""
    with engine.begin() as conn:
        refund_ids = list(conn.scalars(select(refunds.c.refund_id)))
        conn.execute(delete(refunds))
        for row in SEED_INVOICES:
            conn.execute(
                update(invoices)
                .where(invoices.c.invoice_id == row["invoice_id"])
                .values(payment_status=row["payment_status"])
            )
    return refund_ids


# --- Queries ---------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _one(conn: Connection, stmt) -> dict | None:
    row = conn.execute(stmt).mappings().first()
    return dict(row) if row else None


def get_invoice(conn: Connection, invoice_id: str) -> dict | None:
    return _one(conn, select(invoices).where(invoices.c.invoice_id == invoice_id))


def list_invoices(conn: Connection) -> list[dict]:
    rows = conn.execute(select(invoices).order_by(invoices.c.invoice_id)).mappings()
    return [dict(row) for row in rows]


def get_refund(conn: Connection, refund_id: str) -> dict | None:
    return _one(conn, select(refunds).where(refunds.c.refund_id == refund_id))


def insert_refund(conn: Connection, refund_id: str, invoice_id: str, customer_message: str) -> None:
    conn.execute(
        insert(refunds).values(
            refund_id=refund_id,
            invoice_id=invoice_id,
            customer_message=customer_message,
            status=SUBMITTED,
            created_at=utcnow(),
        )
    )


def update_refund(
    conn: Connection,
    refund_id: str,
    *,
    only_if_status: str | Iterable[str] | None = None,
    **values,
) -> bool:
    """Update a refund row, optionally only if its status matches (compare-and-set).

    Returns True if a row changed. A conditional update is atomic, so it doubles as a lock:
    of two concurrent callers, only one sees True.
    """
    stmt = update(refunds).where(refunds.c.refund_id == refund_id).values(**values)
    if only_if_status is not None:
        statuses = [only_if_status] if isinstance(only_if_status, str) else list(only_if_status)
        stmt = stmt.where(refunds.c.status.in_(statuses))
    return conn.execute(stmt).rowcount > 0


def mark_invoice_refunded(conn: Connection, invoice_id: str) -> bool:
    """paid -> refunded. Returns False if the invoice was not 'paid' any more."""
    stmt = (
        update(invoices)
        .where(invoices.c.invoice_id == invoice_id, invoices.c.payment_status == PAID)
        .values(payment_status=INVOICE_REFUNDED)
    )
    return conn.execute(stmt).rowcount > 0


def find_blocking_refund(conn: Connection, invoice_id: str, exclude_refund_id: str) -> dict | None:
    """Another refund for the same invoice that is pending, being decided or already refunded."""
    return _one(
        conn,
        select(refunds.c.refund_id, refunds.c.status)
        .where(
            refunds.c.invoice_id == invoice_id,
            refunds.c.refund_id != exclude_refund_id,
            refunds.c.status.in_(BLOCKING_STATUSES),
        )
        .limit(1),
    )


def list_refunds_with_invoice(conn: Connection, statuses: Iterable[str], newest_first: bool) -> list[dict]:
    """Refund rows joined with their invoice details (invoice amount as invoice_amount)."""
    sort_key = func.coalesce(refunds.c.decided_at, refunds.c.created_at)
    stmt = (
        select(
            refunds,
            invoices.c.customer_name,
            invoices.c.customer_email,
            invoices.c.item,
            invoices.c.quantity,
            invoices.c.amount.label("invoice_amount"),
            invoices.c.currency,
            invoices.c.invoice_date,
            invoices.c.payment_status,
        )
        .join(invoices, refunds.c.invoice_id == invoices.c.invoice_id)
        .where(refunds.c.status.in_(list(statuses)))
        .order_by(sort_key.desc() if newest_first else refunds.c.created_at.asc())
    )
    return [dict(row) for row in conn.execute(stmt).mappings()]


def count_refunds(conn: Connection) -> dict[tuple[str, str | None], int]:
    """Refund counts keyed by (status, processing_type)."""
    stmt = select(refunds.c.status, refunds.c.processing_type, func.count()).group_by(
        refunds.c.status, refunds.c.processing_type
    )
    return {(status, ptype): n for status, ptype, n in conn.execute(stmt)}
