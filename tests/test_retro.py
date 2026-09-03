"""Retrospective grading: the counterfactual replay.

These encode the two mistakes that were actually shipped and then found by
hand, because both produced numbers that looked entirely reasonable:

  * a swing measured on optimal lineups but then applied to the actual score,
    which invented flipped games for a player who never left the bench;
  * a verdict that reported "no games changed hands" while listing two.
"""

from __future__ import annotations

import conftest
import retro
from conftest import LEAGUE_ID


def _played(conn, week, scores, started, team_points, matchups):
    conftest.add_week(conn, week, scores, started, team_points, matchups)


def test_no_games_since_the_trade_means_nothing_to_measure(conn):
    conftest.add_trade(conn, "t1", 1, [("rb3", 1, 2), ("wr2", 2, 1)])
    report = retro.retrospective(conn, LEAGUE_ID, "t1")
    assert report["weeks_measured"] == []
    for side in report["sides"].values():
        assert side["total_swing"] == 0
        assert "No games played" in side["verdict"]


def test_swing_is_zero_when_no_acquired_player_was_started(conn):
    """A player who is acquired and then benched cannot change your score.

    The optimal-basis number may well move -- the roster did get better -- but
    the actual one must not, and it is the actual one that decides games.
    """
    conftest.add_trade(conn, "t1", 1, [("rb3", 1, 2), ("wr2", 2, 1)])
    # Team 1 starts everyone except the player it just acquired.
    _played(
        conn, 2,
        scores={1: {"qb1": 20, "rb1": 15, "wr1": 10, "wr3": 5, "wr2": 30},
                2: {"qb2": 8, "rb2": 12, "te1": 4, "k1": 2, "rb3": 9}},
        started={1: {"qb1", "rb1", "wr1", "wr3"}, 2: {"qb2", "rb2", "te1"}},
        team_points={1: 50, 2: 24},
        matchups={1: 1, 2: 1},
    )
    side = retro.retrospective(conn, LEAGUE_ID, "t1")["sides"][1]
    assert side["total_swing"] == 0
    assert side["flips"] == []
    # The roster ceiling did move, and that is a different question.
    assert side["total_roster_swing"] != 0


def test_a_zero_swing_week_can_never_flip_a_game(conn):
    """The invariant the shipped bug violated."""
    conftest.add_trade(conn, "t1", 1, [("rb3", 1, 2), ("wr2", 2, 1)])
    # Deliberately a one-point loss, so any phantom swing would flip it.
    _played(
        conn, 2,
        scores={1: {"qb1": 20, "rb1": 15, "wr1": 10, "wr3": 5, "wr2": 99},
                2: {"qb2": 8, "rb2": 12, "te1": 4, "k1": 2, "rb3": 9}},
        started={1: {"qb1", "rb1", "wr1", "wr3"}, 2: {"qb2", "rb2", "te1"}},
        team_points={1: 50, 2: 51},
        matchups={1: 1, 2: 1},
    )
    report = retro.retrospective(conn, LEAGUE_ID, "t1")
    for side in report["sides"].values():
        for week in side["weeks"]:
            if week["swing"] == 0:
                assert not any(f["week"] == week["week"] for f in side["flips"])


def test_the_two_swings_are_reported_separately(conn):
    conftest.add_trade(conn, "t1", 1, [("rb3", 1, 2), ("wr2", 2, 1)])
    _played(
        conn, 2,
        scores={1: {"qb1": 20, "rb1": 15, "wr1": 10, "wr3": 5, "wr2": 40},
                2: {"qb2": 8, "rb2": 12, "te1": 4, "k1": 2, "rb3": 9}},
        started={1: {"qb1", "rb1", "wr1", "wr2"}, 2: {"qb2", "rb2", "te1"}},
        team_points={1: 85, 2: 24},
        matchups={1: 1, 2: 1},
    )
    week = retro.retrospective(conn, LEAGUE_ID, "t1")["sides"][1]["weeks"][0]
    assert {"swing", "roster_swing", "without_trade", "actual_scored"} <= week.keys()
    # Started the acquired player, so the actual score really did change.
    assert week["swing"] != 0


def test_a_flip_follows_from_the_numbers_printed_beside_it(conn):
    conftest.add_trade(conn, "t1", 1, [("rb3", 1, 2), ("wr2", 2, 1)])
    _played(
        conn, 2,
        scores={1: {"qb1": 20, "rb1": 15, "wr1": 10, "wr3": 5, "wr2": 40},
                2: {"qb2": 8, "rb2": 12, "te1": 4, "k1": 2, "rb3": 1}},
        started={1: {"qb1", "rb1", "wr1", "wr2"}, 2: {"qb2", "rb2", "te1"}},
        team_points={1: 85, 2: 60},
        matchups={1: 1, 2: 1},
    )
    for side in retro.retrospective(conn, LEAGUE_ID, "t1")["sides"].values():
        for flip in side["flips"]:
            really_won = flip["actual"] > flip["opponent"]
            would_have_won = flip["without_trade"] > flip["opponent"]
            assert really_won != would_have_won


def test_points_only_count_while_the_player_was_yours(conn):
    """Attribution stops when a player leaves; his later points are not yours."""
    conftest.add_trade(conn, "t1", 1, [("rb3", 1, 2), ("wr2", 2, 1)])
    _played(
        conn, 2,
        scores={1: {"qb1": 1, "rb1": 1, "wr1": 1, "wr3": 1, "wr2": 25},
                2: {"qb2": 1, "rb2": 1, "te1": 1, "k1": 1, "rb3": 99}},
        started={1: {"qb1", "rb1", "wr1", "wr2"}, 2: {"qb2", "rb2", "rb3"}},
        team_points={1: 28, 2: 101},
        matchups={1: 1, 2: 1},
    )
    side = retro.retrospective(conn, LEAGUE_ID, "t1")["sides"][1]
    sent = {p["name"]: p for p in side["contributions"]["sent_away"]}
    # rb3 scored 99 for his new team; none of it is credited to team 1.
    assert sent["Charlie Runner"]["started_points"] == 0
    assert sent["Charlie Runner"]["weeks_held"] == 0
    got = {p["name"]: p for p in side["contributions"]["acquired"]}
    assert got["Bravo Catcher"]["started_points"] == 25


def test_verdict_does_not_claim_nothing_changed_when_games_did():
    """The shipped bug: wins and losses that cancel out are still changes."""
    entry = {
        "total_swing": -10.0,
        "weeks": [{}, {}],
        "flips": [{"to": "win"}, {"to": "loss"}],
    }
    verdict = retro._verdict(entry)
    assert "no games changed hands" not in verdict
    assert "1 won" in verdict and "1 lost" in verdict


def test_verdict_wording_survives_points_and_wins_disagreeing():
    # Losing points overall while still buying a win is a real outcome, and
    # "Worth -12.8 points and 1 extra win" is not a sentence.
    verdict = retro._verdict(
        {"total_swing": -12.8, "weeks": [{}], "flips": [{"to": "win"}]}
    )
    assert "Worth -" not in verdict
    assert "win" in verdict


def test_without_trade_leaves_an_untouched_lineup_alone():
    slots = ["QB", "RB", "RB", "WR", "FLEX"]
    positions = {"a": "QB", "b": "RB", "c": "RB", "d": "WR", "e": "WR", "z": "RB"}
    scores = {k: 10.0 for k in positions}
    scores["z"] = 99.0
    started = {k: 10.0 for k in "abcde"}
    # Nothing acquired was started, so the score is unchanged.
    assert retro._without_trade(started, ["z"], set(positions), scores, positions, slots) == 50.0


def test_without_trade_refills_only_the_slot_the_trade_emptied():
    slots = ["QB", "RB", "RB", "WR", "FLEX"]
    positions = {"a": "QB", "b": "RB", "c": "RB", "d": "WR", "e": "WR", "z": "WR"}
    scores = {k: 10.0 for k in positions}
    scores["z"] = 99.0
    started = {"a": 10.0, "b": 10.0, "c": 10.0, "d": 10.0, "z": 99.0}
    # z is removed and the vacated slot refilled by e, the best left.
    assert retro._without_trade(
        started, ["z"], set("abcde"), scores, positions, slots
    ) == 50.0
