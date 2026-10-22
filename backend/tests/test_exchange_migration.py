"""真实执行增量Alembic升级/回滚，确保旧订单与退款记录仍可读。"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path


def test_exchange_migration_keeps_legacy_data_and_refund_index(tmp_path):
    database = tmp_path / "migration.db"
    env = os.environ | {
        "DATABASE_URL": "sqlite+aiosqlite:///" + database.as_posix(),
        "SEED_ON_STARTUP": "false",
    }
    backend = Path(__file__).resolve().parents[1]

    def migrate(*args):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=backend,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    migrate("upgrade", "0002")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO customers VALUES ('old', 'Original', 'old@example.test', 'standard', '2026-10-01')"
        )
        connection.execute(
            "INSERT INTO orders VALUES ('old-order', 'old', 'Legacy product', 'general', 10, 'delivered', '2026-10-01', '2026-10-02', 0, 1)"
        )
        connection.execute(
            "INSERT INTO refunds (order_id, amount, decision, reason, decided_by, created_at) VALUES ('old-order', 10, 'approved', 'original', 'agent', '2026-10-03')"
        )
    migrate("upgrade", "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT product_name, refunded, product_id FROM orders WHERE id='old-order'"
        ).fetchone() == ("Legacy product", 1, None)
        assert connection.execute("SELECT reason FROM refunds").fetchone() == ("original",)
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert {
            "products",
            "after_sales_tickets",
            "ticket_approvals",
            "exchange_acceptances",
            "ticket_events",
            "ticket_requests",
            "conversation_summaries",
            "knowledge_documents",
        } <= tables
        assert connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='uq_refunds_one_approved_per_order'"
        ).fetchone()
    migrate("downgrade", "0002")
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT product_name FROM orders WHERE id='old-order'"
        ).fetchone() == ("Legacy product",)
        assert connection.execute("SELECT count(*) FROM refunds").fetchone() == (1,)
    migrate("upgrade", "head")
