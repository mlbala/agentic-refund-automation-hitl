-- Refund Automation Agent: PostgreSQL schema and demo data.
--
-- Alternative to `uv run python scripts/init_db.py` for creating the tables by hand
-- (psql, the Aiven console, DBeaver, ...). Safe to run more than once.
--
--   psql "host=<host> port=<port> dbname=refund_automation user=<user> sslmode=require" -f scripts/schema.sql
--
-- App tables:        invoices, refunds
--                    (default names; if you set INVOICES_TABLE / REFUNDS_TABLE in .env,
--                    replace "invoices" / "refunds" below with the same names)
-- LangGraph tables:  checkpoints, checkpoint_blobs, checkpoint_writes, checkpoint_migrations
--                    (fixed names; the agent's saved state, which lets a paused refund survive restarts)

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

-- 40 demo invoices (existing rows are left alone). Generated from SEED_INVOICES in db.py.
INSERT INTO invoices
    (invoice_id, customer_name, customer_email, item, quantity, amount, currency, invoice_date, payment_status)
VALUES
    ('INV-1001', 'Alice Martin',    'alice.martin@example.com',    'Wireless mouse',              1,   25.00, 'USD', '2026-08-03', 'paid'),
    ('INV-1002', 'Ben Carter',      'ben.carter@example.com',      'Mechanical keyboard',         1,   80.00, 'USD', '2026-08-07', 'paid'),
    ('INV-1003', 'Chloe Nguyen',    'chloe.nguyen@example.com',    'Headphones',                  1,  100.00, 'USD', '2026-08-12', 'paid'),
    ('INV-1004', 'Dev Patel',       'dev.patel@example.com',       '27" monitor',                 1,  250.00, 'USD', '2026-08-15', 'paid'),
    ('INV-1005', 'Emma Rossi',      'emma.rossi@example.com',      'Office chair',                1,  420.00, 'USD', '2026-08-20', 'paid'),
    ('INV-1006', 'Farid Khan',      'farid.khan@example.com',      'USB-C hub',                   1,   45.00, 'USD', '2026-08-24', 'paid'),
    ('INV-1007', 'Grace Lee',       'grace.lee@example.com',       'Laptop stand',                2,  150.00, 'USD', '2026-09-01', 'paid'),
    ('INV-1008', 'Hiro Tanaka',     'hiro.tanaka@example.com',     'Webcam',                      1,   60.00, 'USD', '2026-09-05', 'refunded'),
    ('INV-1009', 'Isabel Garcia',   'isabel.garcia@example.com',   'Laptop sleeve',               1,   35.00, 'USD', '2026-09-06', 'paid'),
    ('INV-1010', 'Jamal Wright',    'jamal.wright@example.com',    'Portable SSD 1TB',            1,  119.99, 'USD', '2026-09-06', 'paid'),
    ('INV-1011', 'Keiko Sato',      'keiko.sato@example.com',      'Desk lamp',                   1,   42.50, 'USD', '2026-09-07', 'paid'),
    ('INV-1012', 'Liam O''Brien',   'liam.obrien@example.com',     'Ergonomic keyboard',          1,  129.00, 'USD', '2026-09-08', 'paid'),
    ('INV-1013', 'Maria Silva',     'maria.silva@example.com',     'HDMI cable (2 m)',            3,   29.97, 'USD', '2026-09-08', 'paid'),
    ('INV-1014', 'Noah Kim',        'noah.kim@example.com',        'Standing desk',               1,  549.00, 'USD', '2026-09-09', 'paid'),
    ('INV-1015', 'Olivia Brown',    'olivia.brown@example.com',    'Wireless earbuds',            1,   99.99, 'USD', '2026-09-10', 'paid'),
    ('INV-1016', 'Pedro Alvarez',   'pedro.alvarez@example.com',   'Mouse pad XL',                2,   30.00, 'USD', '2026-09-10', 'paid'),
    ('INV-1017', 'Quinn Taylor',    'quinn.taylor@example.com',    '34" ultrawide monitor',       1,  699.00, 'USD', '2026-09-11', 'paid'),
    ('INV-1018', 'Rosa Martinez',   'rosa.martinez@example.com',   'Webcam light',                1,   24.99, 'USD', '2026-09-12', 'refunded'),
    ('INV-1019', 'Sanjay Gupta',    'sanjay.gupta@example.com',    'Docking station',             1,  189.00, 'USD', '2026-09-12', 'paid'),
    ('INV-1020', 'Tara Wilson',     'tara.wilson@example.com',     'Phone stand',                 1,   15.00, 'USD', '2026-09-13', 'paid'),
    ('INV-1021', 'Umar Farooq',     'umar.farooq@example.com',     'Noise-cancelling headphones', 1,  329.00, 'USD', '2026-09-14', 'paid'),
    ('INV-1022', 'Valentina Rossi', 'valentina.rossi@example.com', 'Keyboard wrist rest',         1,   19.99, 'USD', '2026-09-14', 'paid'),
    ('INV-1023', 'William Chen',    'william.chen@example.com',    'Graphics tablet',             1,  100.01, 'USD', '2026-09-15', 'paid'),
    ('INV-1024', 'Xin Li',          'xin.li@example.com',          'USB microphone',              1,   89.00, 'USD', '2026-09-16', 'paid'),
    ('INV-1025', 'Yara Haddad',     'yara.haddad@example.com',     'Office chair mat',            1,   65.00, 'USD', '2026-09-16', 'paid'),
    ('INV-1026', 'Zoe Adams',       'zoe.adams@example.com',       'Keyboard switch set',         4,   48.00, 'USD', '2026-09-17', 'paid'),
    ('INV-1027', 'Aaron Cohen',     'aaron.cohen@example.com',     'Laptop',                      1, 1299.00, 'USD', '2026-09-18', 'paid'),
    ('INV-1028', 'Bianca Ferreira', 'bianca.ferreira@example.com', 'Surge protector',             2,   59.98, 'USD', '2026-09-18', 'paid'),
    ('INV-1029', 'Carlos Mendes',   'carlos.mendes@example.com',   'Monitor arm',                 1,  139.00, 'USD', '2026-09-19', 'refunded'),
    ('INV-1030', 'Diana Petrova',   'diana.petrova@example.com',   'Bluetooth speaker',           1,   75.00, 'USD', '2026-09-19', 'paid'),
    ('INV-1031', 'Ethan Brooks',    'ethan.brooks@example.com',    'External hard drive 4TB',     1,  109.00, 'USD', '2026-09-20', 'paid'),
    ('INV-1032', 'Fatima Zahra',    'fatima.zahra@example.com',    'Webcam cover (5-pack)',       1,    9.99, 'USD', '2026-09-21', 'paid'),
    ('INV-1033', 'George Miller',   'george.miller@example.com',   'Laser printer',               1,  229.00, 'USD', '2026-09-21', 'paid'),
    ('INV-1034', 'Hana Yoshida',    'hana.yoshida@example.com',    'Ink cartridges',              3,   87.00, 'USD', '2026-09-22', 'paid'),
    ('INV-1035', 'Ivan Novak',      'ivan.novak@example.com',      'Wi-Fi router',                1,  159.00, 'USD', '2026-09-23', 'paid'),
    ('INV-1036', 'Julia Schmidt',   'julia.schmidt@example.com',   'Cable organizer kit',         1,   22.00, 'USD', '2026-09-23', 'paid'),
    ('INV-1037', 'Kofi Mensah',     'kofi.mensah@example.com',     'Tablet',                      1,  449.00, 'USD', '2026-09-24', 'refunded'),
    ('INV-1038', 'Lucia Romano',    'lucia.romano@example.com',    'Stylus pen',                  1,  100.00, 'USD', '2026-09-24', 'paid'),
    ('INV-1039', 'Mohammed Ali',    'mohammed.ali@example.com',    'Gaming mouse',                1,   69.99, 'USD', '2026-09-25', 'paid'),
    ('INV-1040', 'Nina Johansson',  'nina.johansson@example.com',  'Conference speakerphone',     1,  249.00, 'USD', '2026-09-25', 'paid')
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
