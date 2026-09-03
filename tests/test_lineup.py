"""The lineup solver.

Everything downstream leans on this: power rankings, the fit half of a trade
grade, and the retrospective replay all call it. If it picks a wrong lineup,
every number in the app is wrong in a way that still looks plausible.
"""

from __future__ import annotations

import analytics


def _c(player_id, position, score):
    return {"player_id": player_id, "position": position, "score": score}


def test_bench_and_taxi_slots_are_not_scoring_slots():
    assert analytics.starting_slots(["QB", "RB", "BN", "TAXI", "IR", "FLEX"]) == [
        "QB", "RB", "FLEX"
    ]


def test_fills_each_slot_with_the_best_eligible_player():
    chosen, total = analytics.best_lineup(
        [_c("a", "QB", 10), _c("b", "QB", 5), _c("c", "RB", 8)], ["QB", "RB"]
    )
    assert total == 18
    assert {c["player_id"] for c in chosen} == {"a", "c"}


def test_a_flex_does_not_steal_a_player_a_fixed_slot_needs():
    # The whole reason slots are filled most-restrictive-first. Filling FLEX
    # first would take the running back and leave the RB slot empty.
    chosen, total = analytics.best_lineup(
        [_c("rb", "RB", 20), _c("wr", "WR", 5)], ["RB", "FLEX"]
    )
    assert total == 25
    slots = {c["player_id"]: c["slot"] for c in chosen}
    assert slots == {"rb": "RB", "wr": "FLEX"}


def test_flex_eligibility_excludes_quarterbacks():
    chosen, total = analytics.best_lineup(
        [_c("qb", "QB", 99), _c("wr", "WR", 3)], ["FLEX"]
    )
    assert total == 3
    assert chosen[0]["player_id"] == "wr"


def test_superflex_does_include_quarterbacks():
    chosen, _ = analytics.best_lineup(
        [_c("qb", "QB", 99), _c("wr", "WR", 3)], ["SUPER_FLEX"]
    )
    assert chosen[0]["player_id"] == "qb"


def test_an_unfillable_slot_scores_nothing_rather_than_raising():
    # A roster with no tight end must still produce a lineup; the TE slot just
    # contributes zero. Raising here would break a whole page.
    chosen, total = analytics.best_lineup([_c("rb", "RB", 10)], ["RB", "TE"])
    assert total == 10
    assert len(chosen) == 1


def test_chosen_players_come_back_in_lineup_order():
    chosen, _ = analytics.best_lineup(
        [_c("a", "RB", 1), _c("b", "QB", 1), _c("c", "WR", 1)], ["QB", "RB", "WR"]
    )
    assert [c["slot"] for c in chosen] == ["QB", "RB", "WR"]


def test_no_player_is_used_twice():
    chosen, _ = analytics.best_lineup(
        [_c("rb", "RB", 10)], ["RB", "RB", "FLEX"]
    )
    assert len(chosen) == 1


def test_empty_roster_scores_zero():
    chosen, total = analytics.best_lineup([], ["QB", "RB"])
    assert (chosen, total) == ([], 0)
