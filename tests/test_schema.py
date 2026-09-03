"""Schema and migrations.

The web app reads the database directly, so a deploy that adds a table has to
migrate on its own. Shipping one that did not caused live 500s.
"""

from __future__ import annotations

import sqlite3

import db


def test_init_db_is_idempotent(db_path):
    for _ in range(3):
        db.init_db().close()
    conn = db.connect()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"leagues", "player_values", "player_weeks", "pick_ownership"} <= tables


def test_a_database_missing_a_later_table_is_migrated(db_path):
    """Reproduces the bug: an older database served 500s until it was synced."""
    db.init_db().close()
    conn = db.connect()
    conn.execute("DROP TABLE pick_ownership")
    conn.commit()
    conn.close()

    with __import__("pytest").raises(sqlite3.OperationalError):
        conn = db.connect()
        try:
            conn.execute("SELECT 1 FROM pick_ownership").fetchone()
        finally:
            conn.close()

    db.init_db().close()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM pick_ownership").fetchone()[0] == 0
    conn.close()


def test_added_columns_are_backfilled_onto_an_older_table(db_path):
    conn = db.connect()
    conn.execute("DROP TABLE IF EXISTS player_values")
    # The shape this table had before the extra market fields were added.
    conn.execute(
        """CREATE TABLE player_values (
             asof_date TEXT NOT NULL, config_key TEXT NOT NULL,
             player_id TEXT NOT NULL, value INTEGER,
             PRIMARY KEY (asof_date, config_key, player_id))"""
    )
    conn.commit()
    conn.close()

    db.init_db().close()
    conn = db.connect()
    columns = {r[1] for r in conn.execute("PRAGMA table_info(player_values)")}
    conn.close()
    assert {"search_rank", "trade_frequency", "combined_value"} <= columns
