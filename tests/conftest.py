"""A small synthetic league, built directly in SQLite.

No network and no fixtures copied from the live API: every test here is about
*logic* -- lineup solving, grading, counterfactual replay, luck arithmetic --
and that logic should be exercised against numbers small enough to verify by
hand. A ten-team league of real players would make a failure hard to read.

The league is deliberately tiny and regular: 4 teams, a 4-slot lineup, values
that are round numbers, and weekly scores that are easy to add up in your head.
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LEAGUE_ID = "test-league"
CONFIG_KEY = "dyn_1qb_4tm_ppr1"
ASOF = "2026-09-02"

# QB, RB, RB, WR is enough to exercise fixed slots; FLEX adds the interesting
# case where the solver must not spend a flex on a player a dedicated slot needs.
ROSTER_POSITIONS = ["QB", "RB", "RB", "WR", "FLEX", "BN", "BN"]

# player_id -> (name, position, value). Names are distinct on purpose except
# where a test needs an ambiguous prefix.
PLAYERS = {
    "qb1": ("Alpha Passer", "QB", 5000),
    "qb2": ("Bravo Passer", "QB", 1000),
    "qb3": ("Charlie Passer", "QB", 2000),
    "rb1": ("Alpha Runner", "RB", 9000),
    "rb2": ("Bravo Runner", "RB", 6000),
    "rb3": ("Charlie Runner", "RB", 3000),
    "rb4": ("Delta Runner", "RB", 5000),
    "rb5": ("Echo Runner", "RB", 4500),
    "rb6": ("Foxtrot Runner", "RB", 1500),
    "wr1": ("Alpha Catcher", "WR", 8000),
    "wr2": ("Bravo Catcher", "WR", 4000),
    "wr3": ("Charlie Catcher", "WR", 2000),
    "wr4": ("Delta Catcher", "WR", 7000),
    "wr5": ("Echo Catcher", "WR", 3500),
    "te1": ("Alpha Tight", "TE", 2500),
    "k1": ("Alpha Kicker", "K", 0),
}

# roster_id -> the players it holds. Disjoint, because Sleeper guarantees a
# player is on at most one roster in a league and code relies on that.
ROSTERS = {
    1: ["qb1", "rb1", "rb3", "wr1", "wr3"],
    2: ["qb2", "rb2", "wr2", "te1", "k1"],
    3: ["qb3", "rb4", "wr4"],
    4: ["rb5", "rb6", "wr5"],
}


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """Point every module at a throwaway database for the duration of a test."""
    path = tmp_path / "test.db"
    monkeypatch.setenv("FFTA_DB", str(path))
    # db caches DB_PATH at import, so it has to be rebound explicitly.
    import db as db_module
    monkeypatch.setattr(db_module, "DB_PATH", str(path))
    return str(path)


@pytest.fixture
def conn(db_path):
    import db as db_module

    connection = db_module.init_db()
    _seed(connection)
    yield connection
    connection.close()


def _seed(conn) -> None:
    conn.execute(
        """INSERT INTO leagues (league_id, name, season, status, league_type,
                                num_teams, roster_positions, playoff_week_start,
                                trade_deadline, ppr, num_qbs, synced_at)
           VALUES (?, 'Test League', '2026', 'in_season', 2, 4, ?, 15, 11, 1.0, 1, ?)""",
        (LEAGUE_ID, json.dumps(ROSTER_POSITIONS), ASOF),
    )
    for roster_id in ROSTERS:
        conn.execute(
            """INSERT INTO managers (league_id, roster_id, user_id, display_name,
                                     team_name, wins, losses, ties, points_for)
               VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0)""",
            (LEAGUE_ID, roster_id, f"u{roster_id}", f"user{roster_id}",
             f"Team {roster_id}"),
        )
    for pid, (name, position, value) in PLAYERS.items():
        conn.execute(
            "INSERT INTO players (player_id, name, position, team, age) VALUES (?, ?, ?, 'NFL', 25)",
            (pid, name, position),
        )
        conn.execute(
            """INSERT INTO player_values (asof_date, config_key, player_id, value,
                                          overall_rank, position_rank)
               VALUES (?, ?, ?, ?, ?, 1)""",
            (ASOF, CONFIG_KEY, pid, value, 1),
        )
    for roster_id, players in ROSTERS.items():
        for pid in players:
            conn.execute(
                """INSERT INTO roster_slots (league_id, snapshot_date, roster_id,
                                             player_id, slot)
                   VALUES (?, ?, ?, ?, 'active')""",
                (LEAGUE_ID, ASOF, roster_id, pid),
            )
    # Future picks, all still held by their original owner.
    for season in ("2027", "2028"):
        for rnd in (1, 2):
            for roster_id in ROSTERS:
                conn.execute(
                    """INSERT INTO pick_ownership (league_id, season, round,
                                                   original_roster, owner_roster)
                       VALUES (?, ?, ?, ?, ?)""",
                    (LEAGUE_ID, season, rnd, roster_id, roster_id),
                )
    for season, rnd, slot, value in (
        ("2027", 1, "early", 4000), ("2027", 1, "mid", 3000),
        ("2027", 1, "late", 2000), ("2027", 1, None, 3000),
        ("2027", 2, "early", 2000), ("2027", 2, "mid", 1500),
        ("2027", 2, "late", 1000), ("2027", 2, None, 1500),
        ("2028", 1, None, 2500), ("2028", 2, None, 1200),
    ):
        conn.execute(
            """INSERT INTO pick_values (asof_date, config_key, label, season,
                                        round, slot, value)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (ASOF, CONFIG_KEY, f"{season} {rnd} {slot}", season, rnd, slot, value),
        )
    conn.commit()


def add_week(conn, week: int, scores: dict, started: dict, team_points: dict,
             matchups: dict) -> None:
    """Record one played week. ``scores`` is {player_id: points} per roster."""
    for roster_id, players in scores.items():
        for pid, points in players.items():
            conn.execute(
                """INSERT OR REPLACE INTO player_weeks
                   (league_id, week, roster_id, player_id, points, started)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (LEAGUE_ID, week, roster_id, pid, points,
                 int(pid in started.get(roster_id, set()))),
            )
    for roster_id, points in team_points.items():
        conn.execute(
            """INSERT OR REPLACE INTO team_weeks
               (league_id, week, roster_id, matchup_id, points)
               VALUES (?, ?, ?, ?, ?)""",
            (LEAGUE_ID, week, roster_id, matchups[roster_id], points),
        )
    conn.commit()


def add_trade(conn, txn_id: str, week: int, moves: list, created_ms: int = 1000) -> None:
    """``moves`` is [(player_id, from_roster, to_roster), ...]."""
    conn.execute(
        """INSERT OR REPLACE INTO transactions
           (txn_id, league_id, week, type, status, created_ms, payload)
           VALUES (?, ?, ?, 'trade', 'complete', ?, '{}')""",
        (txn_id, LEAGUE_ID, week, created_ms),
    )
    for pid, sender, receiver in moves:
        conn.execute(
            """INSERT OR REPLACE INTO transaction_players
               (txn_id, league_id, player_id, roster_id, direction)
               VALUES (?, ?, ?, ?, 'drop')""",
            (txn_id, LEAGUE_ID, pid, sender),
        )
        conn.execute(
            """INSERT OR REPLACE INTO transaction_players
               (txn_id, league_id, player_id, roster_id, direction)
               VALUES (?, ?, ?, ?, 'add')""",
            (txn_id, LEAGUE_ID, pid, receiver),
        )
    conn.commit()


def add_draft_pick(conn, round_no: int, pick_no: int, roster_id: int,
                   player_id: str, draft_id: str = "test-draft") -> None:
    conn.execute(
        """INSERT OR REPLACE INTO draft_picks
           (league_id, draft_id, season, round, pick_no, roster_id, player_id)
           VALUES (?, ?, '2026', ?, ?, ?, ?)""",
        (LEAGUE_ID, draft_id, round_no, pick_no, roster_id, player_id),
    )
    conn.commit()


def add_adp(conn, player_id: str, adp: float, position_adp: float | None = None) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO player_adp (asof_date, season, player_id, adp, position_adp)
           VALUES (?, '2026', ?, ?, ?)""",
        (ASOF, player_id, adp, position_adp),
    )
    conn.commit()
