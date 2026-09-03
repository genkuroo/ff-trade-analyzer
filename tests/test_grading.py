"""Instant trade grading: value, fit, picks, FAAB, and the guard rails."""

from __future__ import annotations

import pytest

import grading
from conftest import LEAGUE_ID


def _grade(conn, mine, theirs, give, get, **kw):
    sides = grading.sides_from_ids(conn, LEAGUE_ID, mine, theirs, give, get, **kw)
    return grading.grade(conn, LEAGUE_ID, sides, applied=False)


def test_value_delta_is_symmetric(conn):
    result = _grade(conn, 1, 2, ["rb1"], ["rb2"])
    deltas = [s["value_delta"] for s in result["sides"].values()]
    assert sum(deltas) == 0
    # rb1 is 9000 and rb2 is 6000, so team 1 gives up 3000 of market value.
    assert sorted(deltas) == [-3000, 3000]


def test_letter_grades_track_the_share_of_value_moved():
    assert grading.letter(0.30) == "A+"
    assert grading.letter(0.00) == "B-"
    assert grading.letter(-0.30) == "F"
    # Ordering must be monotonic, or the scale means nothing.
    scale = [grading.letter(p / 100) for p in range(-40, 41)]
    assert scale[0] == "F" and scale[-1] == "A+"


def test_fit_measures_the_lineup_not_the_roster(conn):
    # Team 1 swaps its worst receiver (2000, starts at FLEX) for a better one
    # (4000). Both start, so the lineup improves by exactly the difference.
    result = _grade(conn, 1, 2, ["wr3"], ["wr2"])
    assert result["sides"][1]["lineup_delta"] == pytest.approx(2000)


def test_value_and_fit_can_point_opposite_ways(conn):
    """The reason the two grades are never blended.

    Team 1 sends its best running back (9000) for a cheaper receiver (7000):
    it loses market value, but wr4 displaces a much weaker starter, so the
    startable lineup can still improve. One combined number would hide that.
    """
    result = _grade(conn, 1, 3, ["rb1"], ["wr4"])
    mine = result["sides"][1]
    assert mine["value_delta"] < 0
    assert mine["value_grade"] != mine["fit_grade"]


def test_applied_flag_direction_is_not_symmetric(conn):
    """The bug this exists for: a proposal graded as if already applied
    silently returns a fit delta of zero, which looks like a real answer."""
    sides = grading.sides_from_ids(conn, LEAGUE_ID, 1, 2, ["wr3"], ["wr2"])
    proposal = grading.grade(conn, LEAGUE_ID, sides, applied=False)
    as_if_done = grading.grade(conn, LEAGUE_ID, sides, applied=True)
    assert proposal["sides"][1]["lineup_delta"] != 0
    assert as_if_done["sides"][1]["lineup_delta"] == 0


def test_picks_are_priced_and_tradeable(conn):
    result = _grade(conn, 1, 2, [], [], give_picks=["2027:1:1"], get_picks=["2027:1:2"])
    labels = [a["label"] for a in result["sides"][1]["assets_in"]]
    assert labels == ["2027 round 1"]
    assert result["sides"][1]["assets_in"][0]["value"] > 0


def test_faab_is_priced_off_the_weakest_starter(conn):
    result = _grade(conn, 1, 2, ["rb1"], [], get_faab=50)
    faab = [a for a in result["sides"][1]["assets_in"] if a["kind"] == "faab"]
    assert len(faab) == 1
    assert faab[0]["value"] > 0
    assert "weakest starter" in faab[0]["basis"]


def test_a_pick_you_do_not_own_is_refused(conn):
    with pytest.raises(grading.ProposalError, match="not theirs to trade"):
        grading.sides_from_ids(
            conn, LEAGUE_ID, 1, 2, [], [], give_picks=["2027:1:2"], get_faab=5
        )


def test_a_player_you_do_not_own_is_refused(conn):
    with pytest.raises(grading.ProposalError, match="not on that roster"):
        grading.sides_from_ids(conn, LEAGUE_ID, 1, 2, ["rb2"], ["rb1"])


def test_a_one_sided_trade_is_refused(conn):
    with pytest.raises(grading.ProposalError, match="not receiving"):
        grading.sides_from_ids(conn, LEAGUE_ID, 1, 2, ["rb1"], [])


def test_both_sides_the_same_team_is_refused(conn):
    with pytest.raises(grading.ProposalError, match="two different teams"):
        grading.sides_from_ids(conn, LEAGUE_ID, 1, 1, [], [], give_faab=1, get_faab=1)


def test_resolving_a_player_by_partial_name(conn):
    player_id, roster_id, name = grading.resolve_player(conn, LEAGUE_ID, "Alpha Runner")
    assert (player_id, roster_id, name) == ("rb1", 1, "Alpha Runner")


def test_an_ambiguous_name_raises_rather_than_guessing(conn):
    with pytest.raises(grading.ProposalError, match="several players"):
        grading.resolve_player(conn, LEAGUE_ID, "Passer")


def test_an_unknown_name_raises(conn):
    with pytest.raises(grading.ProposalError, match="No rostered player"):
        grading.resolve_player(conn, LEAGUE_ID, "Nobody At All")


# -- roster_board ------------------------------------------------------
#
# Feeds the trade machine's picker. Has to include everything a team actually
# owns, not just what it can start -- a taxi-squad rookie or an IR stash is
# exactly the kind of asset a real trade moves.


def test_roster_board_includes_taxi_and_reserve_players(conn):
    conn.execute(
        "UPDATE roster_slots SET slot = 'taxi' WHERE league_id = ? AND player_id = 'wr3'",
        (LEAGUE_ID,),
    )
    conn.execute(
        "UPDATE roster_slots SET slot = 'reserve' WHERE league_id = ? AND player_id = 'rb3'",
        (LEAGUE_ID,),
    )
    conn.commit()

    board = {p["player_id"]: p for p in grading.roster_board(conn, LEAGUE_ID, 1)}
    assert set(board) == {"qb1", "rb1", "rb3", "wr1", "wr3"}
    assert board["wr3"]["slot"] == "taxi"
    assert board["rb3"]["slot"] == "reserve"
    assert board["qb1"]["slot"] == "active"


def test_roster_board_is_all_active_for_a_league_with_no_taxi_or_ir(conn):
    # The shape of a redraft league: Sleeper's own taxi/reserve fields come
    # back empty, so ingest never writes anything but 'active' -- this should
    # behave exactly as it did before taxi/IR support existed, not break.
    board = grading.roster_board(conn, LEAGUE_ID, 2)
    assert board
    assert all(p["slot"] == "active" for p in board)
