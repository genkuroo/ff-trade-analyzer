"""Retrospective ("ex-post") trade grading -- who actually won it.

The instant grade in ``grading.py`` asks what each side gave up at the moment
of the trade. This asks a harder and more interesting question: once the games
were played, did the trade actually help?

Most tools that attempt this just add up the points the traded players scored
afterwards, which is close to meaningless. A receiver who drops 25 on your
bench earned you nothing, and "my guy outscored your guy" says nothing about
whether your *team* was better off -- if you traded a running back you were
never going to start, losing that production cost you zero.

So this replays the season instead. For every week after the trade, it builds
the roster the team *would* have had if the trade had never happened, solves
the best legal lineup for that roster using the points players actually scored
that week, and compares it to the best legal lineup of the roster they really
had. The difference is what the trade was worth, in points that could actually
have reached a lineup.

Two different swings come out of this, and conflating them produces nonsense:

  * **swing** (the headline) is measured on what actually happened. It keeps
    every lineup decision the manager really made, removes the players the
    trade brought in, and refills only the slots that vacates from the
    pre-trade roster. This is "what would my score have been that week", and it
    is the only basis on which a won/lost game can honestly be recomputed.
  * **roster_swing** compares best-possible to best-possible. It measures how
    much better the *roster* got, independent of whether the manager used it.

They can disagree sharply, and the disagreement is informative: acquiring a
player who improves your ceiling but who you never start is a real roster
upgrade worth zero actual points. An earlier version of this file computed only
the optimal-basis swing and then subtracted it from the actual score to decide
which games flipped -- which invented flips for players who never left the
bench. The two bases must not be mixed.

Lineup efficiency (``actual_scored / actual_best``) is reported alongside, since
it is genuinely interesting -- just not the trade's fault.

What this deliberately does not model: that a manager's *other* moves would
have been different without the trade.

The counterfactual roster is built from the roster as it really is today, minus
what the trade brought in, plus what it sent away. That real roster already
contains every waiver pickup made since -- including pickups made specifically
to cover the hole the trade created. So the counterfactual hands back the
traded-away player *and* keeps the replacement signed to replace him, when in
reality the manager only ever had one of the two.

Roster size is what makes this a real distortion rather than a quibble, and it
is also the handle for fixing it: rosters are capped, so a player handed back in
the counterfactual must be *displacing* a waiver claim, not sitting alongside
it. So whenever giving the traded-away players back would leave the roster
larger than it really was, the most recent post-trade free-agent pickups are
dropped until the size matches -- on the reasoning that those claims were only
possible because the roster had the room.

That is a heuristic: some of those pickups had nothing to do with the trade.
``displaced`` on each week's row says how many were dropped, so its influence is
visible rather than implied.

Worth knowing how much it actually matters, because the answer is "less than you
would think". Across the demo season the correction fires a dozen times and
moves the measured swing by **0.0 points**. The reason is structural: only the
slots a trade vacated get refilled, and that slot goes to the player who was
traded away in 16 of 28 cases and to an existing bench player in the other 12 --
almost never to the recent waiver claim being displaced. A design choice made
for an unrelated reason (refilling only vacated slots, so the manager's other
lineup decisions are preserved) turns out to make the whole model largely immune
to this contamination.

So the correction is kept because it is right in principle and costs nothing,
not because it rescues the numbers. The bias it addresses is real but small.

(The opposite error -- a counterfactual so thin that a lineup slot cannot be
filled at all, scoring zero and flattering the trade -- is possible in shallow
leagues but did not occur once across 46 replayed weeks in the demo.)
"""

from __future__ import annotations

import json

import analytics
import grading


def _last_scored_week(conn, league_id: str) -> int:
    row = conn.execute(
        """SELECT MAX(week) AS w FROM player_weeks
            WHERE league_id = ? AND points IS NOT NULL AND points != 0""",
        (league_id,),
    ).fetchone()
    return row["w"] or 0


def week_points(conn, league_id: str, week: int) -> dict:
    """Every player's actual score in a week, whoever happened to roster them.

    Keyed by player rather than by roster, because the counterfactual needs the
    score of a player who is now on someone else's team.
    """
    return {
        row["player_id"]: row["points"] or 0.0
        for row in conn.execute(
            "SELECT player_id, points FROM player_weeks WHERE league_id = ? AND week = ?",
            (league_id, week),
        )
    }


def roster_in_week(conn, league_id: str, roster_id: int, week: int) -> set:
    """Who a team actually rostered in a given week.

    Taken from the weekly matchup feed rather than from roster snapshots,
    because it is the record of what was true *that week* -- snapshots only
    start when this project did.
    """
    return {
        row["player_id"]
        for row in conn.execute(
            """SELECT player_id FROM player_weeks
                WHERE league_id = ? AND roster_id = ? AND week = ?""",
            (league_id, roster_id, week),
        )
    }


def started_in_week(conn, league_id: str, roster_id: int, week: int) -> dict:
    return {
        row["player_id"]: row["points"] or 0.0
        for row in conn.execute(
            """SELECT player_id, points FROM player_weeks
                WHERE league_id = ? AND roster_id = ? AND week = ? AND started = 1""",
            (league_id, roster_id, week),
        )
    }


def post_trade_pickups(conn, league_id: str, roster_id: int, after_ms: int) -> list:
    """Players this roster added off waivers or free agency after a trade.

    Most recent first, because the newest claim is the one most likely to have
    been made *because* of the trade, and so the first that would not have
    happened without it.
    """
    return [
        row["player_id"]
        for row in conn.execute(
            """SELECT tp.player_id
                 FROM transaction_players tp
                 JOIN transactions t ON t.txn_id = tp.txn_id
                WHERE t.league_id = ? AND t.type IN ('free_agent', 'waiver')
                  AND t.status = 'complete' AND t.created_ms > ?
                  AND tp.roster_id = ? AND tp.direction = 'add'
                ORDER BY t.created_ms DESC""",
            (league_id, after_ms, roster_id),
        )
    ]


def _positions(conn) -> dict:
    return {
        row["player_id"]: row["position"]
        for row in conn.execute("SELECT player_id, position FROM players")
    }


def retrospective(conn, league_id: str, txn_id: str) -> dict:
    """Replay every week since a trade, with and without it having happened."""
    league = analytics.league_row(conn, league_id)
    slots = analytics.starting_slots(json.loads(league["roster_positions"]))
    positions = _positions(conn)

    txn = conn.execute(
        "SELECT week, created_ms FROM transactions WHERE txn_id = ?", (txn_id,)
    ).fetchone()
    if not txn:
        raise ValueError(f"No such transaction: {txn_id}")

    sides = grading.trade_sides(conn, txn_id)
    last_week = _last_scored_week(conn, league_id)
    # A trade made during week N first affects week N+1; nothing before the
    # trade can be attributed to it.
    weeks = list(range(txn["week"] + 1, last_week + 1))

    managers = {
        m["roster_id"]: m
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }

    results = {}
    for roster_id, side in sides.items():
        weekly = []
        pickups = post_trade_pickups(
            conn, league_id, roster_id, txn["created_ms"] or 0
        )
        for week in weeks:
            scores = week_points(conn, league_id, week)
            real = roster_in_week(conn, league_id, roster_id, week)

            # The roster as it would have been: give back what was sent away,
            # take away what was received.
            hypothetical = set(real)
            hypothetical -= set(side.get("players_in", []))
            hypothetical |= {
                pid for pid in side.get("players_out", []) if pid in scores
            }

            # Keep the roster the size it really was. A player handed back must
            # displace a waiver claim the manager could not then have afforded
            # the room to make -- otherwise the counterfactual gets both the
            # traded-away player and the replacement signed to replace him.
            displaced = []
            surplus = len(hypothetical) - len(real)
            for pid in pickups:
                if surplus <= 0:
                    break
                if pid in hypothetical:
                    hypothetical.discard(pid)
                    displaced.append(pid)
                    surplus -= 1

            actual_best = _best(real, scores, positions, slots)
            counterfactual_best = _best(hypothetical, scores, positions, slots)
            started = started_in_week(conn, league_id, roster_id, week)
            actual_scored = round(sum(started.values()), 2)
            without_trade = _without_trade(
                started, side.get("players_in", []), hypothetical,
                scores, positions, slots,
            )

            weekly.append(
                {
                    "week": week,
                    "actual_scored": actual_scored,
                    "without_trade": without_trade,
                    "swing": round(actual_scored - without_trade, 2),
                    "actual_best": actual_best,
                    "counterfactual_best": counterfactual_best,
                    "roster_swing": round(actual_best - counterfactual_best, 2),
                    "lineup_efficiency": round(actual_scored / actual_best, 4)
                    if actual_best else None,
                    "displaced": len(displaced),
                    # Non-zero means the roster really was over capacity and
                    # some claims had to be rolled back; still non-zero after
                    # the loop means there were not enough pickups to displace.
                    "size_gap": max(surplus, 0),
                }
            )

        manager = managers.get(roster_id)
        total_swing = round(sum(w["swing"] for w in weekly), 2)
        results[roster_id] = {
            "roster_id": roster_id,
            "total_roster_swing": round(sum(w["roster_swing"] for w in weekly), 2),
            "team": (manager["team_name"] or manager["display_name"])
            if manager else f"Roster {roster_id}",
            "weeks": weekly,
            "total_swing": total_swing,
            "avg_swing": round(total_swing / len(weekly), 2) if weekly else 0.0,
            "contributions": _contributions(conn, league_id, roster_id, side, weeks),
        }

    flips = _flipped_games(conn, league_id, results, weeks)
    for roster_id, entry in results.items():
        entry["flips"] = flips.get(roster_id, [])
        entry["record_swing"] = _record_swing(entry["flips"])
        entry["verdict"] = _verdict(entry)

    return {
        "txn_id": txn_id,
        "trade_week": txn["week"],
        "weeks_measured": weeks,
        "sides": results,
        "unresolved_picks": _unresolved_picks(conn, txn_id),
    }


def _without_trade(started, players_in, hypothetical, scores, positions, slots) -> float:
    """What this manager's real lineup would have scored without the trade.

    Every start they actually made is kept, except the players the trade
    brought in. Only the slots that vacates get refilled, and they are filled
    from the roster as it would have been -- the best available, since a
    manager short a starter goes and finds one.

    Keeping the untouched starts is the whole point: it isolates the trade
    instead of re-managing the team, so the resulting score is comparable to
    what really happened and can be used to recompute a head-to-head result.
    """
    incoming = set(players_in)
    kept = {pid: pts for pid, pts in started.items() if pid not in incoming}
    if len(kept) == len(started):
        # Nothing the trade brought in was actually started, so the trade did
        # not change this week's score at all.
        return round(sum(started.values()), 2)

    kept_assigned, kept_total = analytics.best_lineup(
        _candidates(kept, scores, positions), slots
    )
    used = {a["slot_index"] for a in kept_assigned}
    free_slots = [slot for i, slot in enumerate(slots) if i not in used]

    pool = set(hypothetical) - set(kept)
    _, fill_total = analytics.best_lineup(
        _candidates(pool, scores, positions), free_slots
    )
    return round(kept_total + fill_total, 2)


def _candidates(player_ids, scores, positions) -> list:
    return [
        {"player_id": pid, "position": positions.get(pid, ""),
         "score": scores.get(pid, 0.0)}
        for pid in player_ids
    ]


def _best(player_ids, scores, positions, slots) -> float:
    """Best legal lineup score for a set of players in one week."""
    candidates = [
        {"player_id": pid, "position": positions.get(pid, ""),
         "score": scores.get(pid, 0.0)}
        for pid in player_ids
    ]
    _, total = analytics.best_lineup(candidates, slots)
    return total


def _contributions(conn, league_id, roster_id, side, weeks) -> dict:
    """What the individual pieces did, split by whether they were started.

    Bench points are shown but never counted toward the swing -- they are the
    number that makes naive trade graders wrong, so it is worth displaying them
    next to the number that actually matters.
    """
    if not weeks:
        return {"acquired": [], "sent_away": []}

    # One query for the whole window rather than one per player per week. The
    # roster_id filter is the attribution window: a player only counts while he
    # was actually on this team, so points he scored after being dropped or
    # flipped on belong to whoever held him then.
    placeholders = ",".join("?" for _ in weeks)
    rows = conn.execute(
        f"""SELECT pw.player_id, pw.points, pw.started, p.name, p.position
              FROM player_weeks pw
              JOIN players p ON p.player_id = pw.player_id
             WHERE pw.league_id = ? AND pw.roster_id = ?
               AND pw.week IN ({placeholders})""",
        [league_id, roster_id, *weeks],
    ).fetchall()

    tally: dict[str, dict] = {}
    for row in rows:
        entry = tally.setdefault(
            row["player_id"],
            {"player_id": row["player_id"], "name": row["name"],
             "position": row["position"], "started_points": 0.0,
             "bench_points": 0.0, "weeks_held": 0},
        )
        entry["weeks_held"] += 1
        key = "started_points" if row["started"] else "bench_points"
        entry[key] += row["points"] or 0.0

    def totals(player_ids):
        out = []
        for pid in player_ids:
            entry = tally.get(pid)
            if entry is None:
                row = conn.execute(
                    "SELECT name, position FROM players WHERE player_id = ?", (pid,)
                ).fetchone()
                entry = {
                    "player_id": pid, "name": row["name"] if row else pid,
                    "position": row["position"] if row else "",
                    "started_points": 0.0, "bench_points": 0.0, "weeks_held": 0,
                }
            out.append(
                {
                    **entry,
                    "started_points": round(entry["started_points"], 2),
                    "bench_points": round(entry["bench_points"], 2),
                }
            )
        return out

    return {
        "acquired": totals(side.get("players_in", [])),
        "sent_away": totals(side.get("players_out", [])),
    }


def _flipped_games(conn, league_id, results, weeks) -> dict:
    """Weeks where the trade changed the head-to-head result.

    This is the number people actually argue about. A trade worth 12 points a
    week is abstract; a trade that turned week 6 from a loss into a win is not.

    The opponent's score is held at what it really was. Modelling both sides'
    counterfactuals at once would compound assumptions well past the point of
    being believable.
    """
    flips = {rid: [] for rid in results}
    for week in weeks:
        pairs = {}
        for row in conn.execute(
            """SELECT roster_id, matchup_id, points FROM team_weeks
                WHERE league_id = ? AND week = ? AND matchup_id IS NOT NULL""",
            (league_id, week),
        ):
            pairs.setdefault(row["matchup_id"], []).append(row)

        for teams in pairs.values():
            if len(teams) != 2:
                continue
            for me, them in ((teams[0], teams[1]), (teams[1], teams[0])):
                rid = me["roster_id"]
                if rid not in results:
                    continue
                entry = next(
                    (w for w in results[rid]["weeks"] if w["week"] == week), None
                )
                if not entry:
                    continue
                real_win = (me["points"] or 0) > (them["points"] or 0)
                # Uses the actual-basis counterfactual score directly. Deriving
                # it from the optimal-basis swing would invent flips for players
                # who never left the bench.
                without = entry["without_trade"]
                hypo_win = without > (them["points"] or 0)
                if real_win != hypo_win:
                    flips[rid].append(
                        {
                            "week": week,
                            "from": "loss" if real_win else "win",
                            "to": "win" if real_win else "loss",
                            "actual": round(me["points"] or 0, 2),
                            "without_trade": round(without, 2),
                            "opponent": round(them["points"] or 0, 2),
                        }
                    )
    return flips


def _record_swing(flips) -> str:
    gained = sum(1 for f in flips if f["to"] == "win")
    lost = sum(1 for f in flips if f["to"] == "loss")
    if not gained and not lost:
        return "no games changed hands"
    parts = []
    if gained:
        parts.append(f"+{gained} win{'' if gained == 1 else 's'}")
    if lost:
        parts.append(f"-{lost} win{'' if lost == 1 else 's'}")
    return ", ".join(parts)


def _unresolved_picks(conn, txn_id) -> list:
    """Traded picks that have not become players yet.

    A pick cannot be graded retrospectively until someone drafts with it, and
    saying so is better than quietly valuing it at zero.
    """
    return [
        {"season": row["season"], "round": row["round"]}
        for row in conn.execute(
            "SELECT season, round FROM transaction_picks WHERE txn_id = ?", (txn_id,)
        )
    ]


def _verdict(entry) -> str:
    swing, flips = entry["total_swing"], entry["flips"]
    weeks = len(entry["weeks"])
    if not weeks:
        return "No games played since this trade yet."
    won = sum(1 for f in flips if f["to"] == "win")
    lost = sum(1 for f in flips if f["to"] == "loss")
    # The sign of the points and the direction of the wins can disagree, and
    # that case is the interesting one: fantasy is won weekly, so points landing
    # in the right week matter more than points in total.
    if won and not lost:
        if swing > 0:
            return f"Worth {swing:+.1f} points and {won} extra win{'' if won == 1 else 's'}."
        return (
            f"Cost {abs(swing):.1f} points overall but still bought "
            f"{won} win{'' if won == 1 else 's'} — the gains landed in the weeks "
            f"that mattered."
        )
    if lost and not won:
        if swing < 0:
            return f"Cost {abs(swing):.1f} points and {lost} win{'' if lost == 1 else 's'}."
        return (
            f"Gained {swing:.1f} points overall yet still cost "
            f"{lost} win{'' if lost == 1 else 's'} — the points arrived in weeks "
            f"already won or already lost."
        )
    if won and lost:
        # Games moved in both directions and cancelled out. Saying "no games
        # changed hands" here would be flatly untrue.
        return (
            f"Swung {won + lost} game{'' if won + lost == 1 else 's'} "
            f"({won} won, {lost} lost) for a net of {swing:+.1f} points."
        )
    if abs(swing) < 1:
        return "Effectively a wash so far."
    direction = "helped" if swing > 0 else "hurt"
    return (
        f"{direction.capitalize()} by {abs(swing):.1f} points over {weeks} week"
        f"{'' if weeks == 1 else 's'}, but no games changed hands."
    )
