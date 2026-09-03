"""The draft board: column order, snake handling, and the pick-vs-ADP grade."""

from __future__ import annotations

import analytics
import conftest
from conftest import LEAGUE_ID


def _snake_draft(conn):
    """Two rounds, four teams, a real reversal -- round 1 goes 1,2,3,4 and
    round 2 goes 4,3,2,1, exactly like a real snake draft's second round."""
    picks = [
        (1, 1, 1, "qb1"), (1, 2, 2, "qb2"), (1, 3, 3, "qb3"), (1, 4, 4, "rb5"),
        (2, 5, 4, "rb6"), (2, 6, 3, "rb4"), (2, 7, 2, "rb2"), (2, 8, 1, "rb1"),
    ]
    for round_no, pick_no, roster_id, player_id in picks:
        conftest.add_draft_pick(conn, round_no, pick_no, roster_id, player_id)


def test_column_order_comes_from_round_one(conn):
    _snake_draft(conn)
    board = analytics.draft_board(conn, LEAGUE_ID)
    assert [c["roster_id"] for c in board["columns"]] == [1, 2, 3, 4]


def test_round_two_lands_in_the_same_column_as_round_one_despite_reversing(conn):
    """The whole point of deriving columns from round 1: a snake draft's
    second round must still land under the *same team's* column, not wherever
    it happened to be picked in sequence."""
    _snake_draft(conn)
    board = analytics.draft_board(conn, LEAGUE_ID)
    round1, round2 = board["rounds"][0], board["rounds"][1]

    # Roster 1 picked first in round 1 (column 0) and last in round 2.
    assert round1["picks"][0]["roster_id"] == 1
    assert round2["picks"][0]["roster_id"] == 1
    assert round1["picks"][0]["name"] == "Alpha Passer"   # qb1
    assert round2["picks"][0]["name"] == "Alpha Runner"   # rb1, picked 8th

    # Roster 4 picked last in round 1 and first in round 2 -- same column.
    assert round1["picks"][3]["roster_id"] == 4
    assert round2["picks"][3]["roster_id"] == 4


def test_no_cell_is_ever_left_empty_on_a_complete_draft(conn):
    _snake_draft(conn)
    board = analytics.draft_board(conn, LEAGUE_ID)
    assert all(p is not None for rnd in board["rounds"] for p in rnd["picks"])


def test_adp_delta_is_pick_minus_adp(conn):
    _snake_draft(conn)
    conftest.add_adp(conn, "rb5", adp=12.0)
    board = analytics.draft_board(conn, LEAGUE_ID)
    pick = next(
        p for rnd in board["rounds"] for p in rnd["picks"] if p["player_id"] == "rb5"
    )
    assert pick["pick_no"] == 4
    assert pick["adp_delta"] == 4 - 12.0


def test_no_adp_on_record_grades_as_ungraded_not_zero(conn):
    _snake_draft(conn)
    board = analytics.draft_board(conn, LEAGUE_ID)
    pick = next(
        p for rnd in board["rounds"] for p in rnd["picks"] if p["player_id"] == "qb1"
    )
    assert pick["adp_delta"] is None


def test_a_team_defense_gets_no_headshot(conn):
    """A team defense's player_id is a team abbreviation, not a real Sleeper
    player id -- there is no photo behind it, and the CDN 403s rather than
    404s, so this has to be suppressed by position rather than discovered by
    a broken image in the browser."""
    conn.execute(
        "INSERT OR REPLACE INTO players (player_id, name, position, team) "
        "VALUES ('SEA', 'Seattle Seahawks', 'DEF', 'SEA')"
    )
    conftest.add_draft_pick(conn, 1, 1, 1, "SEA")
    board = analytics.draft_board(conn, LEAGUE_ID)
    pick = board["rounds"][0]["picks"][0]
    assert pick["position"] == "DEF"
    assert pick["headshot"] is None


def test_a_real_player_does_get_a_headshot(conn):
    _snake_draft(conn)
    board = analytics.draft_board(conn, LEAGUE_ID)
    pick = next(
        p for rnd in board["rounds"] for p in rnd["picks"] if p["player_id"] == "qb1"
    )
    assert pick["headshot"] == "https://sleepercdn.com/content/nfl/players/thumb/qb1.jpg"


def test_no_draft_on_record_returns_an_empty_board_not_a_crash(conn):
    board = analytics.draft_board(conn, LEAGUE_ID)
    assert board == {"rounds": [], "columns": [], "asof": board["asof"]}
    assert analytics.draft_team_summary(conn, LEAGUE_ID) == []


def test_team_summary_ranks_by_average_not_raw_total(conn):
    """A team with one huge steal but few graded picks should still out-rank
    a team with a bigger, noisier total spread across more picks -- proving
    the ranking actually flips relative to what sorting by the raw sum would
    produce, not just that the average is computed correctly."""
    _snake_draft(conn)
    # roster 2: one pick graded, one enormous steal -> sum = avg = 50.
    conftest.add_adp(conn, "qb2", adp=-48.0)  # pick 2, delta = 2 - (-48) = 50
    # roster 3: two picks graded, a bigger sum (60) but a lower average (30).
    conftest.add_adp(conn, "qb3", adp=-37.0)  # pick 3, delta = 40
    conftest.add_adp(conn, "rb4", adp=-14.0)  # pick 6, delta = 20

    ranked = analytics.draft_team_summary(conn, LEAGUE_ID)
    roster_ids = [s["roster_id"] for s in ranked]
    by_id = {s["roster_id"]: s for s in ranked}

    assert by_id[2]["adp_delta_sum"] < by_id[3]["adp_delta_sum"]   # 50 < 60
    assert by_id[2]["adp_delta_avg"] > by_id[3]["adp_delta_avg"]   # 50.0 > 30.0

    # A sum-based ranking would put roster 3 first; the actual ranking (by
    # average) must put roster 2 first instead.
    assert roster_ids.index(2) < roster_ids.index(3)


def test_the_draft_page_renders(conn):
    import app as web

    _snake_draft(conn)
    client = web.app.test_client()
    body = client.get(f"/league/{LEAGUE_ID}/draft").data.decode()
    assert "Alpha Passer" in body
    assert "draftgrid" in body


def test_the_draft_page_404s_with_no_draft_on_record(conn):
    import app as web

    client = web.app.test_client()
    assert client.get(f"/league/{LEAGUE_ID}/draft").status_code == 404
