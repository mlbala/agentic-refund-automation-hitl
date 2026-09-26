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

from .config import load_table_names, sqlalchemy_url

# Refund statuses: [queued ->] submitted -> pending_approval -> deciding -> refunded | rejected,
# plus declined (agent found it ineligible) and failed (error).
# queued: saved for the daily batch run; the agent hasn't seen it yet.
# rejected: declined by a human reviewer (the UI shows it as "Declined (reviewer)").
QUEUED = "queued"
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

# Table names come from .env (INVOICES_TABLE, REFUNDS_TABLE); defaults: invoices, refunds.
INVOICES_TABLE, REFUNDS_TABLE = load_table_names()

metadata = MetaData()

invoices = Table(
    INVOICES_TABLE,
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
    REFUNDS_TABLE,
    metadata,
    Column("refund_id", Text, primary_key=True),  # also the LangGraph thread_id
    Column("invoice_id", Text, ForeignKey(invoices.c.invoice_id), nullable=False),
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
    # More demo data: a mix of STP, threshold edge cases ($99.99 / $100.00 / $100.01),
    # approvals up to $1,299 and a few invoices that were already refunded.
    _invoice("INV-1009", "Isabel Garcia", "isabel.garcia@example.com", "Laptop sleeve", 1, "35.00", date(2026, 9, 6)),
    _invoice("INV-1010", "Jamal Wright", "jamal.wright@example.com", "Portable SSD 1TB", 1, "119.99", date(2026, 9, 6)),
    _invoice("INV-1011", "Keiko Sato", "keiko.sato@example.com", "Desk lamp", 1, "42.50", date(2026, 9, 7)),
    _invoice("INV-1012", "Liam O'Brien", "liam.obrien@example.com", "Ergonomic keyboard", 1, "129.00", date(2026, 9, 8)),
    _invoice("INV-1013", "Maria Silva", "maria.silva@example.com", "HDMI cable (2 m)", 3, "29.97", date(2026, 9, 8)),
    _invoice("INV-1014", "Noah Kim", "noah.kim@example.com", "Standing desk", 1, "549.00", date(2026, 9, 9)),
    _invoice("INV-1015", "Olivia Brown", "olivia.brown@example.com", "Wireless earbuds", 1, "99.99", date(2026, 9, 10)),
    _invoice("INV-1016", "Pedro Alvarez", "pedro.alvarez@example.com", "Mouse pad XL", 2, "30.00", date(2026, 9, 10)),
    _invoice("INV-1017", "Quinn Taylor", "quinn.taylor@example.com", '34" ultrawide monitor', 1, "699.00", date(2026, 9, 11)),
    _invoice("INV-1018", "Rosa Martinez", "rosa.martinez@example.com", "Webcam light", 1, "24.99", date(2026, 9, 12), INVOICE_REFUNDED),
    _invoice("INV-1019", "Sanjay Gupta", "sanjay.gupta@example.com", "Docking station", 1, "189.00", date(2026, 9, 12)),
    _invoice("INV-1020", "Tara Wilson", "tara.wilson@example.com", "Phone stand", 1, "15.00", date(2026, 9, 13)),
    _invoice("INV-1021", "Umar Farooq", "umar.farooq@example.com", "Noise-cancelling headphones", 1, "329.00", date(2026, 9, 14)),
    _invoice("INV-1022", "Valentina Rossi", "valentina.rossi@example.com", "Keyboard wrist rest", 1, "19.99", date(2026, 9, 14)),
    _invoice("INV-1023", "William Chen", "william.chen@example.com", "Graphics tablet", 1, "100.01", date(2026, 9, 15)),
    _invoice("INV-1024", "Xin Li", "xin.li@example.com", "USB microphone", 1, "89.00", date(2026, 9, 16)),
    _invoice("INV-1025", "Yara Haddad", "yara.haddad@example.com", "Office chair mat", 1, "65.00", date(2026, 9, 16)),
    _invoice("INV-1026", "Zoe Adams", "zoe.adams@example.com", "Keyboard switch set", 4, "48.00", date(2026, 9, 17)),
    _invoice("INV-1027", "Aaron Cohen", "aaron.cohen@example.com", "Laptop", 1, "1299.00", date(2026, 9, 18)),
    _invoice("INV-1028", "Bianca Ferreira", "bianca.ferreira@example.com", "Surge protector", 2, "59.98", date(2026, 9, 18)),
    _invoice("INV-1029", "Carlos Mendes", "carlos.mendes@example.com", "Monitor arm", 1, "139.00", date(2026, 9, 19), INVOICE_REFUNDED),
    _invoice("INV-1030", "Diana Petrova", "diana.petrova@example.com", "Bluetooth speaker", 1, "75.00", date(2026, 9, 19)),
    _invoice("INV-1031", "Ethan Brooks", "ethan.brooks@example.com", "External hard drive 4TB", 1, "109.00", date(2026, 9, 20)),
    _invoice("INV-1032", "Fatima Zahra", "fatima.zahra@example.com", "Webcam cover (5-pack)", 1, "9.99", date(2026, 9, 21)),
    _invoice("INV-1033", "George Miller", "george.miller@example.com", "Laser printer", 1, "229.00", date(2026, 9, 21)),
    _invoice("INV-1034", "Hana Yoshida", "hana.yoshida@example.com", "Ink cartridges", 3, "87.00", date(2026, 9, 22)),
    _invoice("INV-1035", "Ivan Novak", "ivan.novak@example.com", "Wi-Fi router", 1, "159.00", date(2026, 9, 23)),
    _invoice("INV-1036", "Julia Schmidt", "julia.schmidt@example.com", "Cable organizer kit", 1, "22.00", date(2026, 9, 23)),
    _invoice("INV-1037", "Kofi Mensah", "kofi.mensah@example.com", "Tablet", 1, "449.00", date(2026, 9, 24), INVOICE_REFUNDED),
    _invoice("INV-1038", "Lucia Romano", "lucia.romano@example.com", "Stylus pen", 1, "100.00", date(2026, 9, 24)),
    _invoice("INV-1039", "Mohammed Ali", "mohammed.ali@example.com", "Gaming mouse", 1, "69.99", date(2026, 9, 25)),
    _invoice("INV-1040", "Nina Johansson", "nina.johansson@example.com", "Conference speakerphone", 1, "249.00", date(2026, 9, 25)),
    # Recent invoices (2026-09-25 and 2026-09-26): 9 automatic, 9 needing approval, 2 already refunded.
    _invoice("INV-1041", "Priya Sharma", "priya.sharma@example.com", "Bluetooth keyboard", 1, "49.99", date(2026, 9, 25)),
    _invoice("INV-1042", "Lucas Moreau", "lucas.moreau@example.com", "4K webcam", 1, "159.00", date(2026, 9, 25)),
    _invoice("INV-1043", "Amara Okafor", "amara.okafor@example.com", "Laptop backpack", 1, "64.50", date(2026, 9, 25)),
    _invoice("INV-1044", "Daniel Kowalski", "daniel.kowalski@example.com", "Mechanical keyboard", 1, "139.99", date(2026, 9, 25)),
    _invoice("INV-1045", "Sofia Hernandez", "sofia.hernandez@example.com", "USB-C charger 65W", 2, "79.98", date(2026, 9, 25)),
    _invoice("INV-1046", "Arjun Mehta", "arjun.mehta@example.com", '27" 4K monitor', 1, "379.00", date(2026, 9, 25)),
    _invoice("INV-1047", "Chen Wei", "chen.wei@example.com", "Wireless charging pad", 1, "29.99", date(2026, 9, 25), INVOICE_REFUNDED),
    _invoice("INV-1048", "Hannah Fischer", "hannah.fischer@example.com", "Ergonomic mouse", 1, "99.99", date(2026, 9, 25)),
    _invoice("INV-1049", "Mateo Ruiz", "mateo.ruiz@example.com", "Desk shelf", 1, "100.00", date(2026, 9, 25)),
    _invoice("INV-1050", "Aisha Bello", "aisha.bello@example.com", "Smartwatch", 1, "249.00", date(2026, 9, 25)),
    _invoice("INV-1051", "Ravi Kumar", "ravi.kumar@example.com", "Portable monitor", 1, "189.99", date(2026, 9, 26)),
    _invoice("INV-1052", "Emily Clarke", "emily.clarke@example.com", "Earbud tips (3-pack)", 1, "12.99", date(2026, 9, 26)),
    _invoice("INV-1053", "Tomas Horvat", "tomas.horvat@example.com", "NAS drive enclosure", 1, "299.00", date(2026, 9, 26)),
    _invoice("INV-1054", "Leila Nasser", "leila.nasser@example.com", "Monitor light bar", 1, "55.00", date(2026, 9, 26)),
    _invoice("INV-1055", "Benjamin Scott", "benjamin.scott@example.com", "Office chair cushion", 2, "70.00", date(2026, 9, 26)),
    _invoice("INV-1056", "Mei Suzuki", "mei.suzuki@example.com", "Drawing tablet", 1, "129.00", date(2026, 9, 26), INVOICE_REFUNDED),
    _invoice("INV-1057", "Gabriel Costa", "gabriel.costa@example.com", "SSD 2TB", 1, "169.00", date(2026, 9, 26)),
    _invoice("INV-1058", "Freya Nilsson", "freya.nilsson@example.com", "USB hub 7-port", 1, "39.99", date(2026, 9, 26)),
    _invoice("INV-1059", "Kwame Asante", "kwame.asante@example.com", "Noise-cancelling headset", 1, "219.00", date(2026, 9, 26)),
    _invoice("INV-1060", "Isla McDonald", "isla.mcdonald@example.com", "Laptop stand", 1, "45.00", date(2026, 9, 26)),
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


def insert_refund(
    conn: Connection, refund_id: str, invoice_id: str, customer_message: str, status: str = SUBMITTED
) -> None:
    conn.execute(
        insert(refunds).values(
            refund_id=refund_id,
            invoice_id=invoice_id,
            customer_message=customer_message,
            status=status,
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


def list_refunds_with_invoice(
    conn: Connection,
    statuses: Iterable[str],
    newest_first: bool,
    created_from: datetime | None = None,
    created_before: datetime | None = None,
) -> list[dict]:
    """Refund rows joined with their invoice details (invoice amount as invoice_amount).

    created_from / created_before optionally limit the rows to a time window (e.g. one day).
    """
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
    if created_from is not None:
        stmt = stmt.where(refunds.c.created_at >= created_from)
    if created_before is not None:
        stmt = stmt.where(refunds.c.created_at < created_before)
    return [dict(row) for row in conn.execute(stmt).mappings()]


def count_refunds(conn: Connection) -> dict[tuple[str, str | None], int]:
    """Refund counts keyed by (status, processing_type)."""
    stmt = select(refunds.c.status, refunds.c.processing_type, func.count()).group_by(
        refunds.c.status, refunds.c.processing_type
    )
    return {(status, ptype): n for status, ptype, n in conn.execute(stmt)}
