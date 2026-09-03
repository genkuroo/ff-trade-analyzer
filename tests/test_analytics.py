"""Standings arithmetic, roster history, and per-player movement."""

from __future__ import annotations

import analytics
import conftest
from conftest import ASOF, CONFIG_KEY, LEAGUE_ID


def _week(conn, week, team_points, matchups):
    scores = {rid: {p: 1.0 for p in players} for rid, players in conftest.ROSTERS.items()}
    started = {rid: set(players) for rid, players in conftest.ROSTERS.items()}
    conftest.add_week(conn, week, scores, started, team_points, matchups)


def test_all_play_counts_every_other_team_every_week(conn):
    # One week, four teams. The top scorer beats three, the bottom beats none.
    _week(conn, 1, {1: 100, 2: 90, 3: 80, 4: 70}, {1: 1, 2: 1, 3: 2, 4: 2})
    report = {t["team"]: t for t in analytics.season_report(conn, LEAGUE_ID)}
    assert report["Team 1"]["all_play"] == "3-0"
    assert report["Team 4"]["all_play"] == "0-3"
    assert report["Team 2"]["all_play"] == "2-1"


def test_luck_is_wins_minus_what_the_scoring_earned(conn):
    # Team 3 outscores two teams but is matched against Team 4 and wins;
    # Team 2 outscores one and loses to Team 1.
    _week(conn, 1, {1: 100, 2: 90, 3: 80, 4: 70}, {1: 1, 2: 1, 3: 2, 4: 2})
    report = {t["team"]: t for t in analytics.season_report(conn, LEAGUE_ID)}
    for team in report.values():
        assert team["luck"] == round(team["wins"] - team["expected_wins"], 2)
    # Winning while third-best in scoring is luck by definition.
    assert report["Team 3"]["luck"] > 0


def test_ranking_is_by_all_play_not_by_record(conn):
    _week(conn, 1, {1: 100, 2: 90, 3: 80, 4: 70}, {1: 1, 2: 1, 3: 2, 4: 2})
    report = analytics.season_report(conn, LEAGUE_ID)
    assert [t["team"] for t in report] == ["Team 1", "Team 2", "Team 3", "Team 4"]
    # Team 3 won its game but ranks below Team 2, which lost.
    by_team = {t["team"]: t for t in report}
    assert by_team["Team 3"]["wins"] == 1 and by_team["Team 2"]["wins"] == 0
    assert by_team["Team 3"]["rank"] > by_team["Team 2"]["rank"]


def test_rank_gap_is_positive_when_the_standings_flatter_a_team(conn):
    _week(conn, 1, {1: 100, 2: 90, 3: 80, 4: 70}, {1: 1, 2: 1, 3: 2, 4: 2})
    by_team = {t["team"]: t for t in analytics.season_report(conn, LEAGUE_ID)}
    assert by_team["Team 3"]["rank_gap"] > 0
    assert by_team["Team 2"]["rank_gap"] < 0


def test_no_games_played_returns_nothing_rather_than_zeroes(conn):
    assert analytics.season_report(conn, LEAGUE_ID) == []


def test_a_stint_is_a_run_of_consecutive_weeks(conn):
    """A player who leaves and returns has two stints, not one long one."""
    for week in (1, 2, 5, 6):
        conftest.add_week(
            conn, week,
            {1: {"rb1": 1.0}}, {1: {"rb1"}}, {1: 10}, {1: 1},
        )
    stints = [s for s in analytics.roster_stints(conn, LEAGUE_ID) if s["player_id"] == "rb1"]
    assert len(stints) == 2
    assert (stints[0]["first_week"], stints[0]["last_week"]) == (1, 2)
    assert (stints[1]["first_week"], stints[1]["last_week"]) == (5, 6)


def test_only_the_final_stint_counts_as_active(conn):
    for week in (1, 2, 5, 6):
        conftest.add_week(conn, week, {1: {"rb1": 1.0}}, {1: {"rb1"}}, {1: 10}, {1: 1})
    stints = [s for s in analytics.roster_stints(conn, LEAGUE_ID) if s["player_id"] == "rb1"]
    assert [s["active"] for s in stints] == [False, True]


def test_player_report_derives_movement_from_two_snapshots(conn):
    conn.execute(
        """INSERT INTO player_values (asof_date, config_key, player_id, value,
                                      overall_rank, position_rank)
           VALUES ('2026-09-03', ?, 'rb1', 10000, 1, 1)""",
        (CONFIG_KEY,),
    )
    conn.commit()
    rows = {r["name"]: r for r in analytics.player_report(conn, LEAGUE_ID, window_days=1)}
    runner = rows["Alpha Runner"]
    assert runner["prior_value"] == 9000
    assert runner["value"] == 10000
    assert runner["value_delta"] == 1000
    assert runner["value_delta_pct"] == round(1000 / 9000, 4)


def test_no_earlier_snapshot_means_no_delta_rather_than_zero(conn):
    rows = {r["name"]: r for r in analytics.player_report(conn, LEAGUE_ID, window_days=7)}
    assert rows["Alpha Runner"]["value_delta"] is None


def test_power_rankings_rank_by_startable_lineup_not_roster_total(conn):
    ranked = analytics.power_rankings(conn, LEAGUE_ID)
    assert [t["rank"] for t in ranked] == [1, 2, 3, 4]
    assert all(
        ranked[i]["lineup_value"] >= ranked[i + 1]["lineup_value"]
        for i in range(len(ranked) - 1)
    )
    # Depth that cannot be started still counts toward total value.
    for team in ranked:
        assert team["total_value"] >= team["lineup_value"]


def test_no_trend_without_a_second_snapshot(conn):
    ranked = analytics.power_rankings(conn, LEAGUE_ID)
    assert all(t["trend_available"] is False for t in ranked)
    assert all(t["lineup_delta"] is None for t in ranked)


def _freeze_prices(conn, player_ids, on_date):
    """Copy today's value for each player onto an earlier date, unchanged.

    Isolates a test to the one price move it actually cares about -- without
    this, any roster player missing an earlier-date row defaults to a value of
    zero on that date (the same COALESCE the app itself uses), which swamps
    the comparison with a fake price crash.
    """
    for pid in player_ids:
        conn.execute(
            """INSERT INTO player_values (asof_date, config_key, player_id, value,
                                          overall_rank, position_rank)
               VALUES (?, ?, ?,
                       (SELECT value FROM player_values
                         WHERE config_key = ? AND asof_date = ? AND player_id = ?),
                       1, 1)""",
            (on_date, CONFIG_KEY, pid, CONFIG_KEY, ASOF, pid),
        )


def test_trend_reflects_a_price_move_on_the_same_roster(conn):
    # Roster 1 (qb1, rb1, rb3, wr1, wr3) exactly fills its five lineup slots,
    # so every one of them starts. Freeze all five, then move only rb1.
    _freeze_prices(conn, ["qb1", "rb1", "rb3", "wr1", "wr3"], "2026-08-24")
    conn.execute(
        "UPDATE player_values SET value = 7000 "
        "WHERE config_key = ? AND asof_date = '2026-08-24' AND player_id = 'rb1'",
        (CONFIG_KEY,),
    )
    conn.commit()

    team = next(t for t in analytics.power_rankings(conn, LEAGUE_ID) if t["roster_id"] == 1)
    assert team["trend_available"] is True
    assert team["trend_from"] == "2026-08-24"
    # rb1 was 9000, is now still 9000 today; nine days ago it was 7000.
    assert team["lineup_delta"] == 2000
    assert team["bench_delta"] == 0


def test_bench_delta_is_separate_from_lineup_delta(conn):
    """A player who never starts should move bench_delta, not lineup_delta."""
    _freeze_prices(conn, ["qb1", "rb1", "rb3", "wr1", "wr3"], "2026-08-24")
    # A sixth player, added only to this test, priced low enough that it can
    # never beat the five players who already fill every slot -- so it sits on
    # the bench at both dates regardless of its own price.
    conn.execute(
        "INSERT INTO players (player_id, name, position, team, age) "
        "VALUES ('bench1', 'Bench Player', 'RB', 'NFL', 24)"
    )
    conn.execute(
        "INSERT INTO roster_slots (league_id, snapshot_date, roster_id, player_id, slot) "
        "VALUES (?, ?, 1, 'bench1', 'active')",
        (LEAGUE_ID, ASOF),
    )
    conn.execute(
        "INSERT INTO player_values (asof_date, config_key, player_id, value, overall_rank, position_rank) "
        "VALUES (?, ?, 'bench1', 100, 1, 1)",
        (ASOF, CONFIG_KEY),
    )
    conn.execute(
        "INSERT INTO player_values (asof_date, config_key, player_id, value, overall_rank, position_rank) "
        "VALUES ('2026-08-24', ?, 'bench1', 300, 1, 1)",
        (CONFIG_KEY,),
    )
    conn.commit()

    team = next(t for t in analytics.power_rankings(conn, LEAGUE_ID) if t["roster_id"] == 1)
    assert team["lineup_delta"] == 0
    assert team["bench_delta"] == 100 - 300





# -- avatars ------------------------------------------------------------
#
# Only an id is ever stored; the CDN path is built at render time. One real
# case forces the URL-passthrough branch: Sleeper lets a manager upload a
# custom team logo, and metadata.avatar then holds a full URL rather than an
# opaque id -- observed on the real Money Hole league, not hypothetical.


def test_avatar_url_builds_the_thumb_path_for_a_bare_id():
    assert analytics.avatar_url("abc123") == "https://sleepercdn.com/avatars/thumbs/abc123"


def test_avatar_url_can_request_the_full_size():
    assert analytics.avatar_url("abc123", size="full") == "https://sleepercdn.com/avatars/abc123"


def test_avatar_url_passes_an_uploaded_url_through_unchanged():
    url = "https://sleepercdn.com/uploads/2f149e379b2a66da97b9b51f5d9de1e7.jpg"
    assert analytics.avatar_url(url) == url


def test_avatar_url_is_none_for_no_avatar():
    assert analytics.avatar_url(None) is None
    assert analytics.avatar_url("") is None


def test_power_rankings_carries_each_teams_avatar(conn):
    conn.execute(
        "UPDATE managers SET avatar_id = 'abc123' WHERE league_id = ? AND roster_id = 1",
        (LEAGUE_ID,),
    )
    conn.commit()
    team = next(t for t in analytics.power_rankings(conn, LEAGUE_ID) if t["roster_id"] == 1)
    assert team["avatar"] == "https://sleepercdn.com/avatars/thumbs/abc123"


def test_a_team_with_no_avatar_id_gets_none_not_a_broken_url(conn):
    team = next(t for t in analytics.power_rankings(conn, LEAGUE_ID) if t["roster_id"] == 2)
    assert team["avatar"] is None


def test_league_summary_carries_the_league_avatar(conn):
    conn.execute(
        "UPDATE leagues SET avatar_id = 'league123' WHERE league_id = ?", (LEAGUE_ID,)
    )
    conn.commit()
    assert analytics.league_summary(conn, LEAGUE_ID)["avatar"] == (
        "https://sleepercdn.com/avatars/thumbs/league123"
    )
