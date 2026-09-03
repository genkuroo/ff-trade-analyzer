"""Parsing the market's draft-pick labels."""

from __future__ import annotations

import pytest

from sources import values


@pytest.mark.parametrize(
    "label,expected",
    [
        ("2026 Pick 1.01", ("2026", 1, "01")),
        ("2026 Pick 2.12", ("2026", 2, "12")),
        ("2027 1st (Early)", ("2027", 1, "early")),
        ("2027 2nd (Late)", ("2027", 2, "late")),
        ("2028 1st", ("2028", 1, None)),
        ("2029 4th", ("2029", 4, None)),
    ],
)
def test_known_label_shapes(label, expected):
    assert values.parse_pick_label(label) == expected


def test_an_unknown_shape_degrades_instead_of_raising():
    # A new format upstream should cost one pick's value, not a whole sync.
    assert values.parse_pick_label("2030 supplemental something") == (None, None, None)


def test_config_key_encodes_the_league_shape():
    assert values.config_key(True, 1, 10, 1.0) == "dyn_1qb_10tm_ppr1"
    assert values.config_key(False, 2, 12, 0.5) == "red_2qb_12tm_ppr0.5"


def test_split_separates_picks_from_players():
    rows = [
        {"player": {"position": "PICK", "name": "2027 1st (Early)"}, "value": 4000},
        {"player": {"position": "RB", "name": "Someone", "sleeperId": "42"},
         "value": 9000, "overallRank": 1, "positionRank": 1},
    ]
    players, picks = values.split(rows)
    assert [p["player_id"] for p in players] == ["42"]
    assert [p["round"] for p in picks] == [1]


def test_a_player_without_a_sleeper_id_is_dropped():
    # Without it there is nothing to join to, so the row is unusable.
    players, _ = values.split([{"player": {"position": "WR", "name": "X"}, "value": 1}])
    assert players == []
