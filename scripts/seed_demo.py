"""Build a synthetic league with real trades in it, for demoing the grader.

Two reasons this exists rather than pointing the demo at the real league:

  * **Money Hole has no trades.** It is a first-year dynasty league with an
    empty transaction history, so the trades page has nothing to show and the
    grader has nothing to grade. Waiting for the league to trade is not a
    development plan.
  * **Picks and FAAB have never moved in it either.** Those are the two hardest
    assets to value -- a pick has no slot until the season plays out, and FAAB
    has no market price at all -- and neither code path can be exercised
    against real data yet. The synthetic trades below deliberately include both.

The team names are invented and the trades are invented; only the player pool
and their market values are real, because grading fake players against fake
values would demonstrate nothing.

Safety: this writes to ``demo.db`` unless FFTA_DB says otherwise, so running it
by accident can never touch the real league's database.

    FFTA_DB=demo.db python scripts/seed_demo.py
"""

from __future__ import annotations

import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("FFTA_DB", "demo.db")

import db          # noqa: E402  - must follow the FFTA_DB default above
import ingest      # noqa: E402
from sources import values as valuesrc  # noqa: E402

LEAGUE_ID = "demo-dynasty"
SEED = 20260901

TEAMS = [
    "Autodraft Andy", "The Waiver Wire", "Bench Mob", "Regression Candidates",
    "Play Action Heroes", "Zero RB Truthers", "Injury Report", "Garbage Time",
    "Handcuff Hoarders", "Prevent Defense",
]

ROSTER_POSITIONS = [
    "QB", "RB", "RB", "WR", "WR", "TE", "FLEX", "FLEX",
] + ["BN"] * 10

# Ten weeks is enough for trades made in weeks 2-7 to have several weeks of
# consequences each, which is what the retrospective grader needs to say
# anything.
WEEKS_PLAYED = 10

# Managers do not set optimal lineups. Roughly one start in six is wrong, which
# keeps lineup efficiency realistically in the high 80s and stops the
# retrospective grade's "best possible" numbers from looking identical to what
# was actually scored.
LINEUP_ERROR_RATE = 1 / 6

# Chance each team makes a waiver claim in a given week. Roughly a claim every
# three weeks per manager, which is unremarkable for a real league and is what
# gives the roster-size correction something to correct.
WAIVER_RATE = 0.30

# Each trade is written to exercise a different shape the grader must handle.
TRADES = [
    {
        "week": 2,
        "note": "star for star - the case where value and fit agree",
        "a_gives": ["WR1"], "b_gives": ["RB1"],
    },
    {
        "week": 3,
        "note": "2-for-1 consolidation - depth into a starter",
        "a_gives": ["RB3", "WR4"], "b_gives": ["WR2"],
    },
    {
        "week": 5,
        "note": "rebuild - a veteran star for future picks",
        "a_gives": ["RB2"], "b_gives": [],
        "b_picks": [("2028", 1), ("2029", 1)],
    },
    {
        "week": 7,
        "note": "FAAB sweetener on an otherwise lopsided deal",
        "a_gives": ["TE1"], "b_gives": ["WR5"], "b_faab": 45,
    },
]


def main() -> None:
    random.seed(SEED)
    conn = db.init_db()
    print(f"seeding {os.environ['FFTA_DB']}")

    print("  fetching the real player catalog and value board...")
    ingest.sync_players(conn)
    shape = {"is_dynasty": True, "num_qbs": 1, "num_teams": 10, "ppr": 1.0}
    ingest.sync_values(conn, **shape)
    config_key = valuesrc.config_key(**shape)
    asof = ingest.today()

    _write_league(conn, config_key)
    pool = _pool(conn, config_key, asof)
    rosters = _draft(conn, pool)
    schedule = _write_trades(conn, rosters)
    _simulate_season(conn, rosters, pool, schedule)

    conn.commit()
    trades = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE league_id = ? AND type = 'trade'",
        (LEAGUE_ID,),
    ).fetchone()["n"]
    print(f"  {len(TEAMS)} teams, {sum(len(r) for r in rosters.values())} rostered, {trades} trades")
    print(f"\ndone. view it with:  FFTA_DB={os.environ['FFTA_DB']} python app.py")
    conn.close()


def _write_league(conn, config_key: str) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO leagues
           (league_id, name, season, status, league_type, num_teams,
            roster_positions, playoff_week_start, trade_deadline, ppr, num_qbs,
            previous_league_id, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (LEAGUE_ID, "Demo Dynasty League", "2026", "in_season", 2, len(TEAMS),
         json.dumps(ROSTER_POSITIONS), 15, 11, 1.0, 1, None, ingest.today()),
    )
    # Future picks, so the demo exercises pick valuation and pick trading.
    season = 2026
    rounds = 3
    conn.execute("DELETE FROM pick_ownership WHERE league_id = ?", (LEAGUE_ID,))
    conn.executemany(
        """INSERT OR REPLACE INTO pick_ownership
           (league_id, season, round, original_roster, owner_roster)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (LEAGUE_ID, str(year), rnd, roster, roster)
            for year in range(season + 1, season + 4)
            for rnd in range(1, rounds + 1)
            for roster in range(1, len(TEAMS) + 1)
        ],
    )

    for roster_id, name in enumerate(TEAMS, start=1):
        # Records are invented but internally consistent: they drive the pick
        # tier projection, so a bad team's future first is correctly worth more.
        wins = (roster_id * 3) % 8
        conn.execute(
            """INSERT OR REPLACE INTO managers
               (league_id, roster_id, user_id, display_name, team_name,
                wins, losses, ties, points_for)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (LEAGUE_ID, roster_id, f"demo-user-{roster_id}", name, name,
             wins, 8 - wins, 0, 900 + wins * 45),
        )


def _pool(conn, config_key: str, asof: str) -> dict:
    """Real players grouped by position, best first."""
    pool: dict[str, list] = {}
    for row in conn.execute(
        """SELECT pv.player_id, p.name, p.position, pv.value
             FROM player_values pv JOIN players p ON p.player_id = pv.player_id
            WHERE pv.config_key = ? AND pv.asof_date = ?
              AND p.position IN ('QB','RB','WR','TE')
            ORDER BY pv.value DESC""",
        (config_key, asof),
    ):
        pool.setdefault(row["position"], []).append(dict(row))
    return pool


def _draft(conn, pool: dict) -> dict:
    """Snake-draft the pool so rosters are realistic and roughly balanced."""
    need = {"QB": 2, "RB": 5, "WR": 6, "TE": 2}
    rosters: dict[int, list] = {rid: [] for rid in range(1, len(TEAMS) + 1)}
    cursors = {pos: 0 for pos in need}

    order = list(rosters)
    for position, count in need.items():
        for _ in range(count):
            for roster_id in order:
                index = cursors[position]
                if index >= len(pool.get(position, [])):
                    continue
                rosters[roster_id].append(pool[position][index])
                cursors[position] += 1
            order.reverse()

    snapshot = ingest.today()
    for roster_id, players in rosters.items():
        for player in players:
            conn.execute(
                """INSERT OR REPLACE INTO roster_slots
                   (league_id, snapshot_date, roster_id, player_id, slot)
                   VALUES (?, ?, ?, ?, 'active')""",
                (LEAGUE_ID, snapshot, roster_id, player["player_id"]),
            )
    return rosters


def _weekly_points(value: int, position: str, rng: random.Random) -> float:
    """A plausible weekly score for a player of a given market value.

    Better players score more on average and are no more consistent for it, so
    the noise scales with the mean. The point is not forecasting accuracy -- it
    is that the retrospective grader is fed something with realistic spread,
    since a replay against constant scores would prove nothing.
    """
    if position in ("K", "DEF") or not value:
        base = 7.0
    else:
        base = 3.0 + 15.0 * (value / 11000.0) ** 0.5
    score = rng.gauss(base, base * 0.45)
    return max(0.0, round(score, 2))


def _simulate_season(conn, rosters: dict, pool: dict, schedule: dict) -> None:
    """Play the season week by week, applying trades and waiver claims in order.

    Waiver activity matters here for one specific reason: the retrospective
    grader corrects for roster size by rolling back pickups a manager made
    *after* a trade, on the reasoning that they only had room for the claim
    because they had traded someone away. Without churn in the demo, that whole
    code path would never run.

    Starters are chosen with the same solver the grader uses, then a share are
    deliberately swapped for a bench player at the same position -- otherwise
    every manager is perfectly efficient and the gap between "what they scored"
    and "the best their roster could score" collapses to zero, hiding the
    distinction the grader exists to make.
    """
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import analytics

    rng = random.Random(SEED + 7)
    slots = analytics.starting_slots(ROSTER_POSITIONS)
    ids = sorted(rosters)
    snapshot = ingest.today()
    base_ms = int(time.time() * 1000) - WEEKS_PLAYED * 86_400_000

    drafted = {p["player_id"] for roster in rosters.values() for p in roster}
    free_agents = [
        p for players in pool.values() for p in players
        if p["player_id"] not in drafted
    ]
    # Best available first, so a claim can be a genuine hit rather than
    # guaranteed bench fodder. A waiver pickup that never reaches a lineup
    # cannot exercise the grader's roster-size correction at all, which is the
    # thing this churn exists to test.
    free_agents.sort(key=lambda p: p["value"], reverse=True)
    claims = 0

    for week in range(1, WEEKS_PLAYED + 1):
        # 1. Trades land first, at the week they were made.
        for player, sender, receiver in schedule.get(week, []):
            rosters[sender].remove(player)
            rosters[receiver].append(player)

        # 2. Then waiver claims, which is why they sort after the trade.
        for roster_id in ids:
            if rng.random() > WAIVER_RATE or not free_agents:
                continue
            # Most claims are depth; some are the best thing on the wire.
            add = free_agents.pop(0) if rng.random() < 0.4 else free_agents.pop(
                rng.randrange(min(len(free_agents), 40))
            )
            worst = min(rosters[roster_id], key=lambda p: p["value"])
            rosters[roster_id].remove(worst)
            rosters[roster_id].append(add)
            claims += 1
            txn_id = f"demo-fa-{week}-{roster_id}"
            conn.execute(
                """INSERT OR REPLACE INTO transactions
                   (txn_id, league_id, week, type, status, created_ms, payload)
                   VALUES (?, ?, ?, 'free_agent', 'complete', ?, '{}')""",
                (txn_id, LEAGUE_ID, week, base_ms + week * 86_400_000 + roster_id),
            )
            for player, direction in ((add, "add"), (worst, "drop")):
                conn.execute(
                    """INSERT OR REPLACE INTO transaction_players
                       (txn_id, league_id, player_id, roster_id, direction)
                       VALUES (?, ?, ?, ?, ?)""",
                    (txn_id, LEAGUE_ID, player["player_id"], roster_id, direction),
                )

        # 3. Play the week.
        scores = {}
        for players in rosters.values():
            for p in players:
                scores.setdefault(
                    p["player_id"], _weekly_points(p["value"], p["position"], rng)
                )

        totals = {}
        for roster_id, players in rosters.items():
            candidates = [
                {"player_id": p["player_id"], "position": p["position"],
                 "score": scores[p["player_id"]]}
                for p in players
            ]
            chosen, _ = analytics.best_lineup(candidates, slots)
            started = {c["player_id"] for c in chosen}

            for pick in list(started):
                if rng.random() >= LINEUP_ERROR_RATE:
                    continue
                pos = next(p["position"] for p in players if p["player_id"] == pick)
                bench = [
                    p["player_id"] for p in players
                    if p["position"] == pos and p["player_id"] not in started
                ]
                if bench:
                    started.discard(pick)
                    started.add(rng.choice(bench))

            totals[roster_id] = round(sum(scores[pid] for pid in started), 2)
            for p in players:
                conn.execute(
                    """INSERT OR REPLACE INTO player_weeks
                       (league_id, week, roster_id, player_id, points, started)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (LEAGUE_ID, week, roster_id, p["player_id"],
                     scores[p["player_id"]], int(p["player_id"] in started)),
                )

        order = ids[:]
        rng.shuffle(order)
        for matchup_id, i in enumerate(range(0, len(order), 2), start=1):
            for roster_id in order[i:i + 2]:
                conn.execute(
                    """INSERT OR REPLACE INTO team_weeks
                       (league_id, week, roster_id, matchup_id, points)
                       VALUES (?, ?, ?, ?, ?)""",
                    (LEAGUE_ID, week, roster_id, matchup_id, totals[roster_id]),
                )

    # Final roster state, so the instant grader and the dashboard agree with
    # where everyone ended up.
    conn.execute("DELETE FROM roster_slots WHERE league_id = ?", (LEAGUE_ID,))
    for roster_id, players in rosters.items():
        for p in players:
            conn.execute(
                """INSERT OR REPLACE INTO roster_slots
                   (league_id, snapshot_date, roster_id, player_id, slot)
                   VALUES (?, ?, ?, ?, 'active')""",
                (LEAGUE_ID, snapshot, roster_id, p["player_id"]),
            )
    print(f"  {WEEKS_PLAYED} weeks played, {claims} waiver claims, imperfect lineups")


def _pick_from(roster: list, spec: str) -> dict:
    """Resolve a spec like 'RB2' to that roster's 2nd-best running back."""
    position, rank = spec[:2], int(spec[2:])
    ranked = sorted(
        [p for p in roster if p["position"] == position],
        key=lambda p: p["value"], reverse=True,
    )
    return ranked[rank - 1]


def _write_trades(conn, rosters: dict) -> dict:
    """Write the trade records and return {week: [(player, from, to), ...]}.

    Rosters are deliberately not mutated here. The season loop applies trades
    and waiver claims together in week order, so a pickup made *after* a trade
    really is recorded after it -- which is what the retrospective grader's
    roster-size correction depends on.
    """
    schedule: dict[int, list] = {}
    now_ms = int(time.time() * 1000)
    for index, trade in enumerate(TRADES):
        # Pair up teams from opposite ends of the standings so the rebuild
        # trade has a plausible buyer and seller.
        a, b = 1 + index, len(TEAMS) - index
        txn_id = f"demo-trade-{index + 1}"
        conn.execute(
            """INSERT OR REPLACE INTO transactions
               (txn_id, league_id, week, type, status, created_ms, payload)
               VALUES (?, ?, ?, 'trade', 'complete', ?, ?)""",
            (txn_id, LEAGUE_ID, trade["week"],
             now_ms - (10 - trade["week"]) * 86_400_000,
             json.dumps({"note": trade["note"]})),
        )

        moved = []
        for spec in trade.get("a_gives", []):
            moved.append((_pick_from(rosters[a], spec), a, b))
        for spec in trade.get("b_gives", []):
            moved.append((_pick_from(rosters[b], spec), b, a))
        schedule.setdefault(trade["week"], []).extend(moved)

        for player, sender, receiver in moved:
            conn.execute(
                """INSERT OR REPLACE INTO transaction_players
                   (txn_id, league_id, player_id, roster_id, direction)
                   VALUES (?, ?, ?, ?, 'drop')""",
                (txn_id, LEAGUE_ID, player["player_id"], sender),
            )
            conn.execute(
                """INSERT OR REPLACE INTO transaction_players
                   (txn_id, league_id, player_id, roster_id, direction)
                   VALUES (?, ?, ?, ?, 'add')""",
                (txn_id, LEAGUE_ID, player["player_id"], receiver),
            )

        for season, rnd in trade.get("b_picks", []):
            conn.execute(
                """INSERT OR REPLACE INTO transaction_picks
                   (txn_id, league_id, season, round, original_roster,
                    from_roster, to_roster)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (txn_id, LEAGUE_ID, season, rnd, b, b, a),
            )
        if trade.get("b_faab"):
            conn.execute(
                """INSERT OR REPLACE INTO transaction_faab
                   (txn_id, league_id, from_roster, to_roster, amount)
                   VALUES (?, ?, ?, ?, ?)""",
                (txn_id, LEAGUE_ID, b, a, trade["b_faab"]),
            )
        print(f"  week {trade['week']}: {trade['note']}")
    return schedule


if __name__ == "__main__":
    main()
