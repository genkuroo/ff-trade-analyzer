"""SQLite schema and connection helper for ff-trade-analyzer.

One local database file holds every league's data side by side, so the same
tables serve one league or ten. Every table therefore carries a ``league_id``
except the two that are genuinely global: the NFL player catalog, and the
market value snapshots (which are keyed by league *shape*, not league id, so
two 10-team 1QB PPR dynasty leagues share one set of values).

The table that does the real work is ``player_weeks``. Sleeper hands back each
week's matchups as two parallel blobs -- a ``starters`` array and a
``players_points`` map -- which is awkward to query. Flattening them into one
row per (week, roster, player) with a ``started`` flag is what makes the
retrospective grader a SQL query instead of a pile of JSON parsing.

Schema is created idempotently, so re-running a sync is always safe.
"""

import os
import sqlite3

DB_PATH = os.environ.get("FFTA_DB", os.path.join(os.path.dirname(__file__), "ffta.db"))


def connect():
    """Open a connection with row access by column name and FKs enforced."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn=None):
    """Create all tables if they don't exist. Safe to call on every run.

    Returns an open connection. When none is passed in, one is opened here and
    handed back for the caller to own and close.
    """
    if conn is None:
        conn = connect()

    # -- league metadata ---------------------------------------------------

    # One row per league per season. Sleeper models a dynasty league's seasons
    # as separate leagues chained by previous_league_id, so that column is how
    # we walk history backwards when backfilling.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS leagues (
            league_id           TEXT PRIMARY KEY,
            name                TEXT,
            season              TEXT,
            status              TEXT,
            league_type         INTEGER,   -- 0 redraft, 1 keeper, 2 dynasty
            num_teams           INTEGER,
            roster_positions    TEXT,      -- JSON array, incl. BN/TAXI/IR slots
            playoff_week_start  INTEGER,
            trade_deadline      INTEGER,
            ppr                 REAL,      -- points per reception
            num_qbs             INTEGER,   -- 1 or 2 (superflex), drives valuation
            previous_league_id  TEXT,
            synced_at           TEXT
        )"""
    )

    # Sleeper keeps identity (user_id) and team slot (roster_id) in separate
    # endpoints; joining them once here means nothing downstream has to.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS managers (
            league_id    TEXT NOT NULL,
            roster_id    INTEGER NOT NULL,
            user_id      TEXT,
            display_name TEXT,
            team_name    TEXT,
            wins         INTEGER,
            losses       INTEGER,
            ties         INTEGER,
            points_for   REAL,
            PRIMARY KEY (league_id, roster_id)
        )"""
    )

    # -- global reference --------------------------------------------------

    # The NFL player catalog, shared by every league. Sleeper's own payload is
    # ~5 MB of mostly-unused fields; only what a grade needs is kept.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS players (
            player_id TEXT PRIMARY KEY,   -- Sleeper's id, the join key everywhere
            name      TEXT,
            position  TEXT,
            team      TEXT,
            age       REAL,
            status    TEXT
        )"""
    )

    # Market values from FantasyCalc, snapshotted so a trade can be graded
    # against the values that were true *on the day it happened* rather than
    # today's -- the difference between "was this a good trade" and hindsight.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS player_values (
            asof_date     TEXT NOT NULL,
            config_key    TEXT NOT NULL,   -- e.g. dyn_1qb_10tm_ppr1.0
            player_id     TEXT NOT NULL,   -- Sleeper id
            value         INTEGER,
            redraft_value INTEGER,
            overall_rank  INTEGER,
            position_rank INTEGER,
            trend_30day   INTEGER,
            tier          INTEGER,
            PRIMARY KEY (asof_date, config_key, player_id)
        )"""
    )

    # Draft picks are half of every dynasty trade, so they need values too.
    # FantasyCalc labels them by expected slot ("2027 1st (Early)"), while
    # Sleeper only tells us season + round -- reconciling those is the
    # valuation layer's job, not this table's.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pick_values (
            asof_date  TEXT NOT NULL,
            config_key TEXT NOT NULL,
            label      TEXT NOT NULL,   -- FantasyCalc's raw label
            season     TEXT,
            round      INTEGER,
            slot       TEXT,            -- early/mid/late/exact pick no./NULL
            value      INTEGER,
            PRIMARY KEY (asof_date, config_key, label)
        )"""
    )

    # -- league state ------------------------------------------------------

    # Roster snapshots. Kept dated rather than overwritten so we can answer
    # "who did this team have the week before the trade".
    conn.execute(
        """CREATE TABLE IF NOT EXISTS roster_slots (
            league_id     TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            roster_id     INTEGER NOT NULL,
            player_id     TEXT NOT NULL,
            slot          TEXT,   -- active / taxi / reserve
            PRIMARY KEY (league_id, snapshot_date, roster_id, player_id)
        )"""
    )

    # Every transaction, raw payload retained so a schema change later doesn't
    # require re-fetching a whole season from Sleeper.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transactions (
            txn_id     TEXT PRIMARY KEY,
            league_id  TEXT NOT NULL,
            week       INTEGER,
            type       TEXT,     -- trade / waiver / free_agent
            status     TEXT,
            created_ms INTEGER,
            payload    TEXT      -- original JSON
        )"""
    )

    # Flattened legs of a transaction: one row per player moved.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transaction_players (
            txn_id    TEXT NOT NULL,
            league_id TEXT NOT NULL,
            player_id TEXT NOT NULL,
            roster_id INTEGER NOT NULL,   -- the roster on the receiving/dropping end
            direction TEXT NOT NULL,      -- add / drop
            PRIMARY KEY (txn_id, player_id, direction)
        )"""
    )

    # Draft picks moved in a trade. Sleeper gives season + round + which roster
    # the pick originally belonged to (owner_id is the new owner).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transaction_picks (
            txn_id           TEXT NOT NULL,
            league_id        TEXT NOT NULL,
            season           TEXT,
            round            INTEGER,
            original_roster  INTEGER,   -- whose pick it originally is
            from_roster      INTEGER,
            to_roster        INTEGER,
            PRIMARY KEY (txn_id, season, round, original_roster)
        )"""
    )

    # FAAB moved in a trade, which is real value and often the sweetener.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS transaction_faab (
            txn_id      TEXT NOT NULL,
            league_id   TEXT NOT NULL,
            from_roster INTEGER,
            to_roster   INTEGER,
            amount      INTEGER,
            PRIMARY KEY (txn_id, from_roster, to_roster)
        )"""
    )

    # -- weekly results ----------------------------------------------------

    # The core fact table. One row per player per week per roster, with the
    # points they scored and whether they were actually started. "Scored 25 on
    # your bench" and "scored 25 in your lineup" are different outcomes, and
    # this flag is what lets the grader tell them apart.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS player_weeks (
            league_id  TEXT NOT NULL,
            week       INTEGER NOT NULL,
            roster_id  INTEGER NOT NULL,
            player_id  TEXT NOT NULL,
            points     REAL,
            started    INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (league_id, week, roster_id, player_id)
        )"""
    )

    # Team-level weekly result, kept separately because a team's official score
    # can differ from the sum of its starters (bonuses, corrections).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS team_weeks (
            league_id  TEXT NOT NULL,
            week       INTEGER NOT NULL,
            roster_id  INTEGER NOT NULL,
            matchup_id INTEGER,   -- shared by the two teams playing each other
            points     REAL,
            PRIMARY KEY (league_id, week, roster_id)
        )"""
    )

    # Actual rookie/startup draft results, so a traded pick can be resolved to
    # the player it became -- that is how a pick gets a retrospective grade.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS draft_picks (
            league_id    TEXT NOT NULL,
            draft_id     TEXT NOT NULL,
            season       TEXT,
            round        INTEGER,
            pick_no      INTEGER,
            roster_id    INTEGER,   -- who made the selection
            player_id    TEXT,
            PRIMARY KEY (draft_id, pick_no)
        )"""
    )

    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_pw_player ON player_weeks (league_id, player_id, week)",
        "CREATE INDEX IF NOT EXISTS idx_txn_league ON transactions (league_id, type, week)",
        "CREATE INDEX IF NOT EXISTS idx_tp_txn ON transaction_players (league_id, player_id)",
        "CREATE INDEX IF NOT EXISTS idx_pv_lookup ON player_values (config_key, player_id, asof_date)",
    ):
        conn.execute(stmt)

    conn.commit()
    return conn


if __name__ == "__main__":
    init_db().close()
    print(f"initialized {DB_PATH}")
