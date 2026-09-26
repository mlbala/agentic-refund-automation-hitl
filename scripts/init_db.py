"""Create app tables, seed demo invoices and set up the LangGraph checkpoint tables.

Safe to run repeatedly: existing tables and invoices are left alone.

    uv run python scripts/init_db.py           # create + seed (idempotent)
    uv run python scripts/init_db.py --reset   # also delete all refunds, their agent
                                               # checkpoints, and restore seed invoice statuses
"""

import argparse

from refund_agent.checkpointer import create_checkpointer, create_pool
from refund_agent.config import load_settings
from refund_agent.db import (
    INVOICES_TABLE,
    REFUNDS_TABLE,
    create_db_engine,
    create_tables,
    reset_demo_data,
    seed_invoices,
    upgrade_schema,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--reset", action="store_true", help="wipe refunds and restore demo invoices")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(settings.database_url)

    create_tables(engine)
    for column in upgrade_schema(engine):  # columns added after the first release
        print(f"Added column {column}.")
    print(f"App tables ready: {INVOICES_TABLE}, {REFUNDS_TABLE}.")

    with create_pool(settings.database_url) as pool:
        checkpointer = create_checkpointer(pool)
        checkpointer.setup()  # idempotent migrations for the checkpoint tables
        print("LangGraph checkpoint tables ready.")

        if args.reset:
            refund_ids = reset_demo_data(engine)
            for refund_id in refund_ids:
                checkpointer.delete_thread(refund_id)
            print(f"Reset: removed {len(refund_ids)} refund(s) and restored seed invoice statuses.")

    added = seed_invoices(engine)
    print(f"Seeded {added} new invoice(s).")
    engine.dispose()


if __name__ == "__main__":
    main()
