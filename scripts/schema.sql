-- Refund Automation Agent: PostgreSQL schema and demo data.
--
-- Alternative to `uv run python scripts/init_db.py` for creating the tables by hand
-- (psql, the Aiven console, DBeaver, ...). Safe to run more than once.
--
--   psql "host=<host> port=<port> dbname=refund_automation user=<user> sslmode=require" -f scripts/schema.sql
--
-- App tables:        invoices, refunds
-- LangGraph tables:  checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations
--                    (the agent's saved state; this is what lets a paused refund survive restarts)

BEGIN;

-- ---------------------------------------------------------------------------
-- App tables (mirror src/refund_agent/db.py)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id      TEXT           NOT NULL PRIMARY KEY,   -- e.g. INV-1001
    customer_name   TEXT           NOT NULL,
    customer_email  TEXT           NOT NULL,
    item            TEXT           NOT NULL,
    quantity        INTEGER        NOT NULL,
    amount          NUMERIC(10, 2) NOT NULL,
    currency        TEXT           NOT NULL DEFAULT 'USD',
    invoice_date    DATE           NOT NULL,
    payment_status  TEXT           NOT NULL                -- paid | refunded
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id         TEXT           NOT NULL PRIMARY KEY, -- REF-XXXXXXXX, also the LangGraph thread_id
    invoice_id        TEXT           NOT NULL REFERENCES invoices (invoice_id),
    customer_message  TEXT           NOT NULL,
    amount            NUMERIC(10, 2),                      -- set when the agent decides
    status            TEXT           NOT NULL,             -- submitted | pending_approval | deciding |
                                                           -- refunded | rejected | declined | failed
    processing_type   TEXT,                                -- stp | human_approved
    agent_reason      TEXT,
    agent_summary     TEXT,
    decided_by        TEXT,                                -- ai-agent or reviewer name
    reviewer_note     TEXT,
    created_at        TIMESTAMPTZ    NOT NULL,
    decided_at        TIMESTAMPTZ,
    processed_at      TIMESTAMPTZ
);

-- Demo invoices (existing rows are left alone).
INSERT INTO invoices
    (invoice_id, customer_name, customer_email, item, quantity, amount, currency, invoice_date, payment_status)
VALUES
    ('INV-1001', 'Alice Martin', 'alice.martin@example.com', 'Wireless mouse',      1,  25.00, 'USD', '2026-08-03', 'paid'),
    ('INV-1002', 'Ben Carter',   'ben.carter@example.com',   'Mechanical keyboard', 1,  80.00, 'USD', '2026-08-07', 'paid'),
    ('INV-1003', 'Chloe Nguyen', 'chloe.nguyen@example.com', 'Headphones',          1, 100.00, 'USD', '2026-08-12', 'paid'),
    ('INV-1004', 'Dev Patel',    'dev.patel@example.com',    '27" monitor',         1, 250.00, 'USD', '2026-08-15', 'paid'),
    ('INV-1005', 'Emma Rossi',   'emma.rossi@example.com',   'Office chair',        1, 420.00, 'USD', '2026-08-20', 'paid'),
    ('INV-1006', 'Farid Khan',   'farid.khan@example.com',   'USB-C hub',           1,  45.00, 'USD', '2026-08-24', 'paid'),
    ('INV-1007', 'Grace Lee',    'grace.lee@example.com',    'Laptop stand',        2, 150.00, 'USD', '2026-09-01', 'paid'),
    ('INV-1008', 'Hiro Tanaka',  'hiro.tanaka@example.com',  'Webcam',              1,  60.00, 'USD', '2026-09-05', 'refunded')
ON CONFLICT (invoice_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- LangGraph checkpoint tables (what PostgresSaver.setup() creates,
-- langgraph-checkpoint-postgres 3.1, migrations 0-9)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS checkpoint_migrations (
    v INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id             TEXT  NOT NULL,
    checkpoint_ns         TEXT  NOT NULL DEFAULT '',
    checkpoint_id         TEXT  NOT NULL,
    parent_checkpoint_id  TEXT,
    type                  TEXT,
    checkpoint            JSONB NOT NULL,
    metadata              JSONB NOT NULL DEFAULT '{}',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);

CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id      TEXT  NOT NULL,
    checkpoint_ns  TEXT  NOT NULL DEFAULT '',
    channel        TEXT  NOT NULL,
    version        TEXT  NOT NULL,
    type           TEXT  NOT NULL,
    blob           BYTEA,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);

CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id      TEXT    NOT NULL,
    checkpoint_ns  TEXT    NOT NULL DEFAULT '',
    checkpoint_id  TEXT    NOT NULL,
    task_id        TEXT    NOT NULL,
    idx            INTEGER NOT NULL,
    channel        TEXT    NOT NULL,
    type           TEXT,
    blob           BYTEA   NOT NULL,
    task_path      TEXT    NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);

CREATE INDEX IF NOT EXISTS checkpoints_thread_id_idx       ON checkpoints (thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_blobs_thread_id_idx  ON checkpoint_blobs (thread_id);
CREATE INDEX IF NOT EXISTS checkpoint_writes_thread_id_idx ON checkpoint_writes (thread_id);

-- Mark migrations 0-9 as applied so PostgresSaver.setup() doesn't repeat them.
INSERT INTO checkpoint_migrations (v)
SELECT generate_series(0, 9)
ON CONFLICT (v) DO NOTHING;

COMMIT;
