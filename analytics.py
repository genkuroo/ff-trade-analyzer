"""Read-only computations over the synced database.

Nothing here fetches anything; every function takes a connection and returns
plain dicts. That split is deliberate -- the web app must never be the thing
that calls Sleeper, so a page load can't be slow or rate-limited.

The important piece is :func:`best_lineup`. Fantasy value is not the sum of a
roster, it is the sum of the players you can actually *start*, and the two
diverge sharply for a team with four good running backs and no tight end.
The same solver is what Phase 3 will use to replay a week's scoring with and
without a trade.
"""

from __future__ import annotations

import json

# How far back power_rankings looks to compute each team's value trend.
# Matches the default window on the players page, so "how has this team moved"
# and "how has this player moved" answer the same question.
TREND_WINDOW_DAYS = 7

# Which real positions may fill each lineup slot. Sleeper's own slot names.
SLOT_ELIGIBILITY = {
    "QB": {"QB"},
    "RB": {"RB"},
    "WR": {"WR"},
    "TE": {"TE"},
    "K": {"K"},
    "DEF": {"DEF"},
    "FLEX": {"RB", "WR", "TE"},
    "WRRB_FLEX": {"RB", "WR"},
    "REC_FLEX": {"WR", "TE"},
    "SUPER_FLEX": {"QB", "RB", "WR", "TE"},
    "IDP_FLEX": {"DL", "LB", "DB"},
}

# Slots that hold players but never score.
BENCH_SLOTS = {"BN", "TAXI", "IR"}


def avatar_url(avatar_id: str | None, size: str = "thumb") -> str | None:
    """Sleeper's avatar CDN path for a manager's or league's icon.

    Only the id is stored in the database, never a built URL -- so a CDN
    change is a one-line edit here rather than a backfill of every row. The id
    is normally an opaque hash, but Sleeper has been observed to let a manager
    set a custom team avatar as a full URL in ``metadata.avatar`` rather than
    an uploaded id, so a value that already looks like a URL is passed through
    unchanged instead of being mangled into a broken CDN path.
    """
    if not avatar_id:
        return None
    if avatar_id.startswith(("http://", "https://")):
        return avatar_id
    path = "avatars/thumbs" if size == "thumb" else "avatars"
    return f"https://sleepercdn.com/{path}/{avatar_id}"


# How many colours the initial-circle fallback rotates through. Fixed and
# small on purpose -- the CSS defines exactly this many --avatar-N tokens, one
# set for light mode and one for dark, in the same style as every other themed
# colour in the app.
AVATAR_PALETTE_SIZE = 8


def avatar_fallback(name: str, roster_id: int) -> dict:
    """A deterministic initial-circle to show in place of a missing avatar.

    Not every manager sets a Sleeper profile picture -- Sleeper genuinely
    returns nothing for them, same as their blank icon inside the Sleeper app
    itself. Leaving the space blank reads as broken; a colour is friendlier.

    Keyed off ``roster_id`` rather than the team name, so a mid-season rename
    doesn't reshuffle everyone's colour -- the same team keeps the same one for
    as long as it holds that roster slot.
    """
    initial = next((c for c in (name or "").strip().upper() if c.isalnum()), "?")
    return {"initial": initial, "color_index": roster_id % AVATAR_PALETTE_SIZE}


def starting_slots(roster_positions: list[str]) -> list[str]:
    """The scoring slots of a lineup, bench and taxi removed."""
    return [slot for slot in roster_positions if slot not in BENCH_SLOTS]


def best_lineup(candidates: list[dict], slots: list[str]) -> tuple[list[dict], float]:
    """Fill ``slots`` with the highest-scoring legal assignment of candidates.

    Each candidate is ``{"player_id", "position", "score"}``. Returns the
    chosen players (each tagged with the slot they filled) and the total.

    Slots are filled most-restrictive-first -- a dedicated QB slot before a
    SUPER_FLEX, single-position slots before any flex -- because a flex can
    always take a leftover, while giving a flex the best running back first can
    leave a dedicated RB slot empty. With real lineup sizes (nine or ten slots)
    that ordering produces the true optimum, and it avoids the combinatorial
    search a general solver would need.
    """
    remaining = {c["player_id"]: c for c in candidates}
    ordered = sorted(
        range(len(slots)), key=lambda i: len(SLOT_ELIGIBILITY.get(slots[i], set()))
    )

    chosen: list[dict] = []
    for index in ordered:
        slot = slots[index]
        eligible = SLOT_ELIGIBILITY.get(slot, {slot})
        pool = [c for c in remaining.values() if c["position"] in eligible]
        if not pool:
            continue
        pick = max(pool, key=lambda c: c["score"])
        del remaining[pick["player_id"]]
        chosen.append({**pick, "slot": slot, "slot_index": index})

    chosen.sort(key=lambda c: c["slot_index"])
    return chosen, round(sum(c["score"] for c in chosen), 2)


# -- database-backed views -------------------------------------------------


def league_row(conn, league_id: str):
    return conn.execute(
        "SELECT * FROM leagues WHERE league_id = ?", (league_id,)
    ).fetchone()


def all_leagues(conn) -> list:
    return conn.execute(
        "SELECT * FROM leagues ORDER BY season DESC, name"
    ).fetchall()


def config_key_for(league) -> str:
    kind = "dyn" if league["league_type"] == 2 else "red"
    return f"{kind}_{league['num_qbs']}qb_{league['num_teams']}tm_ppr{league['ppr']:g}"


def latest_value_date(conn, config_key: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(asof_date) AS d FROM player_values WHERE config_key = ?",
        (config_key,),
    ).fetchone()
    return row["d"] if row else None


def power_rankings(conn, league_id: str) -> list[dict]:
    """Rank teams by the value of the lineup they can actually field.

    Two numbers per team, and the gap between them is the story:

      * **lineup** -- the best legal starting lineup they could put out today.
        This is what wins games.
      * **total** -- every player they own, taxi and IR included. This is what
        they could trade.

    A team with a high total and a mediocre lineup is holding depth it cannot
    use, which is exactly the team that should be making a consolidation trade.
    Kickers and defenses come back unvalued from the market data (they have no
    trade value), so they contribute zero to both numbers rather than being
    dropped -- otherwise their lineup slots would look unfillable.

    Each team also carries a **trend**: how its lineup value and its bench
    (depth) value have moved since an earlier snapshot, using the roster as it
    stands today priced at two different dates -- the same method
    ``player_report`` uses for a single player, just run through the lineup
    solver so it reflects the team's actual starters rather than a raw sum.
    Bench movement is reported separately from lineup movement because they
    answer different questions: a team can be getting stronger where it counts
    (lineup) while its unstartable depth quietly loses value, or the reverse.
    With fewer than two snapshots on record there is nothing to compare, and
    that is reported as ``trend_available: False`` rather than a fabricated
    zero.
    """
    league = league_row(conn, league_id)
    if not league:
        return []
    key = config_key_for(league)
    asof = latest_value_date(conn, key)
    slots = starting_slots(json.loads(league["roster_positions"]))
    snapshot = _latest_roster_date(conn, league_id)

    earlier = value_asof(conn, key, _shift(asof, TREND_WINDOW_DAYS)) if asof else None
    trend_available = bool(earlier and earlier != asof)

    rows = conn.execute(
        """SELECT rs.roster_id, rs.player_id, rs.slot AS roster_slot,
                  p.name, p.position, p.team, p.age,
                  COALESCE(pv.value, 0) AS value,
                  COALESCE(pv_then.value, 0) AS value_then
             FROM roster_slots rs
             JOIN players p ON p.player_id = rs.player_id
             LEFT JOIN player_values pv
                    ON pv.player_id = rs.player_id
                   AND pv.config_key = ? AND pv.asof_date = ?
             LEFT JOIN player_values pv_then
                    ON pv_then.player_id = rs.player_id
                   AND pv_then.config_key = ? AND pv_then.asof_date = ?
            WHERE rs.league_id = ? AND rs.snapshot_date = ?""",
        (key, asof, key, earlier or asof, league_id, snapshot),
    ).fetchall()

    managers = {
        m["roster_id"]: m
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }

    by_roster: dict[int, list] = {}
    for row in rows:
        by_roster.setdefault(row["roster_id"], []).append(row)

    ranked = []
    for roster_id, players in by_roster.items():
        manager = managers.get(roster_id) or {}
        # Taxi and IR players cannot be started, so they are worth depth but
        # never lineup points. Counting them as startable would flatter a
        # rebuilding team holding three stashed rookies.
        startable = [
            {"player_id": p["player_id"], "position": p["position"], "score": p["value"],
             "name": p["name"], "team": p["team"], "age": p["age"]}
            for p in players
            if p["roster_slot"] == "active"
        ]
        lineup, lineup_value = best_lineup(startable, slots)
        total_value = sum(p["value"] for p in players)

        lineup_delta = lineup_delta_pct = bench_delta = None
        if trend_available:
            startable_then = [
                {"player_id": p["player_id"], "position": p["position"],
                 "score": p["value_then"]}
                for p in players
                if p["roster_slot"] == "active"
            ]
            _, lineup_value_then = best_lineup(startable_then, slots)
            total_value_then = sum(p["value_then"] for p in players)
            lineup_delta = lineup_value - lineup_value_then
            lineup_delta_pct = (
                round(lineup_delta / lineup_value_then, 4) if lineup_value_then else None
            )
            # What moved outside the lineup -- depth gaining or losing value
            # while the starters stayed flat is a different signal than the
            # lineup itself moving.
            bench_delta = (total_value - lineup_value) - (total_value_then - lineup_value_then)

        team_name = (
            (manager["team_name"] if manager else None)
            or (manager["display_name"] if manager else None)
            or f"Roster {roster_id}"
        )
        ranked.append(
            {
                "roster_id": roster_id,
                "team": team_name,
                "manager": manager["display_name"] if manager else "",
                "avatar": avatar_url(manager["avatar_id"]) if manager else None,
                # Used only when there is no real avatar to show. Keyed off
                # roster_id rather than the name, so it stays the same team's
                # colour even if they rename mid-season.
                "avatar_fallback": avatar_fallback(team_name, roster_id),
                "record": _record(manager),
                "points_for": manager["points_for"] if manager else 0.0,
                "lineup_value": lineup_value,
                "total_value": total_value,
                "lineup": lineup,
                "best_player": max(players, key=lambda p: p["value"])["name"]
                if players else "",
                "depth_ratio": round(
                    (total_value or 1) / (lineup_value or 1), 2
                ),
                "trend_available": trend_available,
                "trend_from": earlier,
                "lineup_delta": lineup_delta,
                "lineup_delta_pct": lineup_delta_pct,
                "bench_delta": bench_delta,
            }
        )

    ranked.sort(key=lambda t: t["lineup_value"], reverse=True)
    for position, team in enumerate(ranked, start=1):
        team["rank"] = position
    return ranked


def _record(manager) -> str:
    if not manager:
        return ""
    ties = f"-{manager['ties']}" if manager["ties"] else ""
    return f"{manager['wins']}-{manager['losses']}{ties}"


def _latest_roster_date(conn, league_id: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM roster_slots WHERE league_id = ?",
        (league_id,),
    ).fetchone()
    return row["d"] if row else None


def recent_transactions(conn, league_id: str, limit: int = 25) -> list[dict]:
    """Latest completed moves, with player names resolved."""
    rows = conn.execute(
        """SELECT t.txn_id, t.week, t.type, t.created_ms
             FROM transactions t
            WHERE t.league_id = ? AND t.status = 'complete'
            ORDER BY t.created_ms DESC LIMIT ?""",
        (league_id, limit),
    ).fetchall()

    managers = {
        m["roster_id"]: (m["team_name"] or m["display_name"])
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }

    out = []
    for row in rows:
        legs = conn.execute(
            """SELECT tp.direction, tp.roster_id, p.name, p.position, p.team
                 FROM transaction_players tp
                 JOIN players p ON p.player_id = tp.player_id
                WHERE tp.txn_id = ?""",
            (row["txn_id"],),
        ).fetchall()
        out.append(
            {
                "txn_id": row["txn_id"],
                "week": row["week"],
                "type": row["type"],
                "created_ms": row["created_ms"],
                "adds": [
                    {"name": l["name"], "position": l["position"],
                     "team": managers.get(l["roster_id"], "?")}
                    for l in legs if l["direction"] == "add"
                ],
                "drops": [
                    {"name": l["name"], "position": l["position"],
                     "team": managers.get(l["roster_id"], "?")}
                    for l in legs if l["direction"] == "drop"
                ],
            }
        )
    return out


def league_summary(conn, league_id: str) -> dict:
    league = league_row(conn, league_id)
    trades = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE league_id = ? AND type = 'trade'",
        (league_id,),
    ).fetchone()["n"]
    weeks = conn.execute(
        "SELECT COUNT(DISTINCT week) AS n FROM player_weeks WHERE league_id = ?",
        (league_id,),
    ).fetchone()["n"]
    return {
        "name": league["name"],
        "season": league["season"],
        "status": league["status"],
        "kind": {0: "redraft", 1: "keeper", 2: "dynasty"}.get(league["league_type"], "?"),
        "teams": league["num_teams"],
        # Most leagues never set a custom league icon; the header falls back
        # to the eyebrow text alone when this is None rather than reserving
        # blank space for an image that will never arrive.
        "avatar": avatar_url(league["avatar_id"]),
        "trades": trades,
        "weeks_scored": weeks,
        "synced_at": league["synced_at"],
        "value_date": latest_value_date(conn, config_key_for(league)),
    }


# -- player-level reporting ------------------------------------------------
#
# Everything below derives its numbers at read time from raw dated rows. No
# delta or percentage is ever stored: a stored delta goes stale the moment
# either endpoint is revised, and keeping only raw observations means any
# window can be asked for later without having committed to one up front.


def value_asof(conn, config_key: str, on_or_before: str) -> str | None:
    """The newest value snapshot at or before a date, so a gap doesn't break a
    comparison -- a missing Tuesday falls back to Monday rather than to null."""
    row = conn.execute(
        """SELECT MAX(asof_date) AS d FROM player_values
            WHERE config_key = ? AND asof_date <= ?""",
        (config_key, on_or_before),
    ).fetchone()
    return row["d"] if row else None


def _shift(date: str, days: int) -> str:
    import datetime as dt
    return (dt.date.fromisoformat(date) - dt.timedelta(days=days)).isoformat()


def player_report(conn, league_id: str, window_days: int = 7,
                  rostered_only: bool = True) -> list[dict]:
    """Raw market observations for every player, alongside derived movement.

    Each row carries the observed numbers (value, ranks, ADP, tier, how often
    the player is actually traded) *and* the movement computed from them: the
    change in value over the window, as points and as a percentage, plus the
    gap between where the market said a player would go and where this league
    actually took him.

    That ADP gap is the interesting one. ADP says where people *do* draft a
    player; overall rank says where they *should*. A player taken well after
    his ADP was either a steal or a read the room disagreed with, and which one
    it was only becomes clear later -- which is why both the raw pick number and
    the raw ADP are kept rather than just the difference.
    """
    league = league_row(conn, league_id)
    config_key = config_key_for(league)
    latest = latest_value_date(conn, config_key)
    if not latest:
        return []
    earlier = value_asof(conn, config_key, _shift(latest, window_days))

    rows = conn.execute(
        """SELECT p.player_id, p.name, p.position, p.team, p.age,
                  now.value, now.combined_value, now.redraft_value,
                  now.overall_rank, now.position_rank, now.tier,
                  now.trend_30day, now.is_starter, now.trade_frequency,
                  now.roster_percent, now.value_stddev_pct, now.search_rank,
                  was.value        AS prior_value,
                  was.overall_rank AS prior_overall_rank,
                  adp.adp, adp.position_adp,
                  dp.pick_no, dp.round AS draft_round,
                  rs.roster_id, m.team_name, m.display_name
             FROM player_values now
             JOIN players p ON p.player_id = now.player_id
             LEFT JOIN player_values was
                    ON was.player_id = now.player_id
                   AND was.config_key = now.config_key
                   AND was.asof_date = ?
             LEFT JOIN player_adp adp
                    ON adp.player_id = now.player_id
                   AND adp.asof_date = (SELECT MAX(asof_date) FROM player_adp)
             LEFT JOIN draft_picks dp
                    ON dp.player_id = now.player_id AND dp.league_id = ?
             LEFT JOIN roster_slots rs
                    ON rs.player_id = now.player_id AND rs.league_id = ?
                   AND rs.snapshot_date =
                       (SELECT MAX(snapshot_date) FROM roster_slots WHERE league_id = ?)
             LEFT JOIN managers m
                    ON m.league_id = ? AND m.roster_id = rs.roster_id
            WHERE now.config_key = ? AND now.asof_date = ?""",
        (earlier, league_id, league_id, league_id, league_id, config_key, latest),
    ).fetchall()

    out = []
    for row in rows:
        if rostered_only and row["roster_id"] is None:
            continue
        value = row["value"] or 0
        prior = row["prior_value"]
        delta = (value - prior) if prior is not None else None
        out.append(
            {
                # --- raw observations -------------------------------------
                "player_id": row["player_id"],
                "name": row["name"],
                "position": row["position"],
                "team": row["team"],
                "age": row["age"],
                "value": value,
                "prior_value": prior,
                "combined_value": row["combined_value"],
                "redraft_value": row["redraft_value"],
                "overall_rank": row["overall_rank"],
                "position_rank": row["position_rank"],
                "search_rank": row["search_rank"],
                "tier": row["tier"],
                "adp": row["adp"],
                "position_adp": row["position_adp"],
                "actual_pick": row["pick_no"],
                "draft_round": row["draft_round"],
                "trend_30day": row["trend_30day"],
                "trade_frequency": row["trade_frequency"],
                "roster_percent": row["roster_percent"],
                "value_stddev_pct": row["value_stddev_pct"],
                "is_starter": bool(row["is_starter"]),
                "roster_id": row["roster_id"],
                "owner": row["team_name"] or row["display_name"],
                # --- derived ----------------------------------------------
                "value_delta": delta,
                "value_delta_pct": round(delta / prior, 4)
                if delta is not None and prior else None,
                "rank_delta": (row["prior_overall_rank"] - row["overall_rank"])
                if row["prior_overall_rank"] and row["overall_rank"] else None,
                # Positive = fell past his ADP (later than expected).
                "adp_delta": round(row["pick_no"] - row["adp"], 1)
                if row["pick_no"] and row["adp"] else None,
            }
        )

    out.sort(key=lambda r: r["value"], reverse=True)
    return out


def draft_report(conn, league_id: str, rows: list[dict] | None = None) -> dict:
    """How this league's draft compares to where the market had players going.

    Only players with both a real pick number and a real ADP can be compared,
    so kickers, defenses and undrafted free agents fall out -- correctly, since
    "undrafted" is not a draft position.

    ``rows`` lets a caller that has already built a report hand it in rather
    than pay for the join twice; the page shows both views of the same data.
    """
    if rows is None:
        rows = player_report(conn, league_id, rostered_only=False)
    players = [row for row in rows if row["adp_delta"] is not None]
    reaches = sorted(players, key=lambda r: r["adp_delta"])[:10]
    steals = sorted(players, key=lambda r: r["adp_delta"], reverse=True)[:10]
    return {
        "compared": len(players),
        "reaches": reaches,   # taken earlier than the market said
        "steals": steals,     # lasted longer than the market said
    }


# -- roster history --------------------------------------------------------


def roster_stints(conn, league_id: str, roster_id: int | None = None) -> list[dict]:
    """Every spell a player has spent on a team: an "all-time roster".

    Two different questions get asked of a roster constantly, and blurring them
    causes bugs: *who is on this team* and *who has ever been on this team*. A
    trade grade needs the second — you cannot credit a player for points he
    scored after you dropped him — while a power ranking needs the first.

    A stint is a run of consecutive weeks. Players do leave and come back, so
    ``MIN(week)``/``MAX(week)`` would silently merge two separate spells into
    one long stint that never happened; the runs are walked explicitly instead.

    Source is ``player_weeks``, which is Sleeper's own weekly record of who
    rostered whom. That makes it authoritative rather than reconstructed, and
    it reaches back further than this project's own daily roster snapshots.
    """
    params = [league_id]
    clause = ""
    if roster_id is not None:
        clause = " AND pw.roster_id = ?"
        params.append(roster_id)

    rows = conn.execute(
        f"""SELECT pw.roster_id, pw.player_id, pw.week,
                   p.name, p.position, p.team
              FROM player_weeks pw
              JOIN players p ON p.player_id = pw.player_id
             WHERE pw.league_id = ?{clause}
             ORDER BY pw.roster_id, pw.player_id, pw.week""",
        params,
    ).fetchall()

    last_week = conn.execute(
        "SELECT MAX(week) AS w FROM player_weeks WHERE league_id = ?", (league_id,)
    ).fetchone()["w"] or 0

    stints: list[dict] = []
    current = None
    for row in rows:
        key = (row["roster_id"], row["player_id"])
        if current and current["_key"] == key and row["week"] == current["last_week"] + 1:
            current["last_week"] = row["week"]
            current["weeks_held"] += 1
            continue
        if current:
            stints.append(current)
        current = {
            "_key": key,
            "roster_id": row["roster_id"],
            "player_id": row["player_id"],
            "name": row["name"],
            "position": row["position"],
            "team": row["team"],
            "first_week": row["week"],
            "last_week": row["week"],
            "weeks_held": 1,
        }
    if current:
        stints.append(current)

    for stint in stints:
        stint.pop("_key")
        stint["active"] = stint["last_week"] == last_week
    return stints


def stint_provenance(conn, league_id: str) -> dict:
    """How each player arrived on each roster: {(roster_id, player_id): type}.

    Draft picks leave no transaction, so anything without one is treated as
    drafted -- which is what "no record of them arriving" means for a roster
    that has held the player since week one.
    """
    out = {}
    for row in conn.execute(
        """SELECT tp.roster_id, tp.player_id, t.type, t.week, t.created_ms
             FROM transaction_players tp
             JOIN transactions t ON t.txn_id = tp.txn_id
            WHERE t.league_id = ? AND tp.direction = 'add' AND t.status = 'complete'
            ORDER BY t.created_ms""",
        (league_id,),
    ):
        out[(row["roster_id"], row["player_id"])] = {
            "via": row["type"],
            "week": row["week"],
            "created_ms": row["created_ms"],
        }
    return out


def all_time_roster(conn, league_id: str, roster_id: int) -> dict:
    """One team's full history, split into who is still here and who is not."""
    provenance = stint_provenance(conn, league_id)
    stints = roster_stints(conn, league_id, roster_id)
    for stint in stints:
        source = provenance.get((roster_id, stint["player_id"]))
        stint["acquired_via"] = source["via"] if source else "draft"
        stint["acquired_week"] = source["week"] if source else 0
    return {
        "active": [s for s in stints if s["active"]],
        "departed": [s for s in stints if not s["active"]],
        "total_held": len(stints),
    }


# -- season performance ----------------------------------------------------


def season_report(conn, league_id: str) -> list[dict]:
    """Rank teams by how good they have actually been, not by their record.

    A fantasy record is part team, part draw. You can score the second-most
    points in the league every week and sit at 3-5 because you kept running
    into whoever went off that week. The fix is the **all-play record**: for
    each week, count how many of the other teams you would have beaten. Over a
    season that removes the schedule entirely, because everyone plays the same
    opponent set -- all of them, every week.

    The gap between actual wins and all-play expected wins *is* luck, in the
    literal sense of outcomes that had nothing to do with how the team played.

    Three other things get reported next to it, deliberately unblended:

      * **lineup efficiency** -- points actually started over the most the
        roster could have scored. This is manager skill, and it is the one
        number here a manager fully controls.
      * **consistency** -- week-to-week standard deviation. A volatile team is
        a worse favourite and a better underdog, which no single ranking
        captures.
      * **market value** -- the forward-looking view from the trade market. A
        team can be playing above its roster and about to fall off.

    Blending these into one score would hide exactly the disagreements that
    make the table worth reading, so they stay in separate columns.
    """
    weeks = [
        row["week"]
        for row in conn.execute(
            """SELECT DISTINCT week FROM team_weeks
                WHERE league_id = ? AND points IS NOT NULL AND points > 0
                ORDER BY week""",
            (league_id,),
        )
    ]
    if not weeks:
        return []

    league = league_row(conn, league_id)
    slots = starting_slots(json.loads(league["roster_positions"]))
    positions = {
        row["player_id"]: row["position"]
        for row in conn.execute("SELECT player_id, position FROM players")
    }
    managers = {
        m["roster_id"]: m
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }

    scores: dict[int, dict[int, float]] = {}
    matchups: dict[int, dict[int, int]] = {}
    for row in conn.execute(
        """SELECT week, roster_id, matchup_id, points FROM team_weeks
            WHERE league_id = ? AND points IS NOT NULL""",
        (league_id,),
    ):
        scores.setdefault(row["week"], {})[row["roster_id"]] = row["points"] or 0.0
        matchups.setdefault(row["week"], {})[row["roster_id"]] = row["matchup_id"]

    optimal = _optimal_by_week(conn, league_id, weeks, positions, slots)

    stats: dict[int, dict] = {}
    for week in weeks:
        week_scores = scores.get(week, {})
        for roster_id, points in week_scores.items():
            entry = stats.setdefault(
                roster_id,
                {"roster_id": roster_id, "scores": [], "optimal": 0.0,
                 "all_play_w": 0, "all_play_l": 0, "wins": 0, "losses": 0,
                 "ties": 0, "points_for": 0.0, "points_against": 0.0},
            )
            entry["scores"].append(points)
            entry["points_for"] += points
            entry["optimal"] += optimal.get((week, roster_id), 0.0)

            # All-play: everyone else's score that week is an opponent.
            for other_id, other in week_scores.items():
                if other_id == roster_id:
                    continue
                if points > other:
                    entry["all_play_w"] += 1
                elif points < other:
                    entry["all_play_l"] += 1

            # The real head-to-head.
            mine = matchups.get(week, {}).get(roster_id)
            opponent = next(
                (r for r, m in matchups.get(week, {}).items()
                 if m == mine and r != roster_id),
                None,
            )
            if opponent is not None:
                against = week_scores.get(opponent, 0.0)
                entry["points_against"] += against
                if points > against:
                    entry["wins"] += 1
                elif points < against:
                    entry["losses"] += 1
                else:
                    entry["ties"] += 1

    ranked = []
    value_by_roster = {t["roster_id"]: t for t in power_rankings(conn, league_id)}
    for roster_id, entry in stats.items():
        games = entry["wins"] + entry["losses"] + entry["ties"]
        all_play_total = entry["all_play_w"] + entry["all_play_l"]
        all_play_pct = (
            entry["all_play_w"] / all_play_total if all_play_total else 0.0
        )
        expected_wins = round(all_play_pct * games, 2)
        manager = managers.get(roster_id)
        value = value_by_roster.get(roster_id, {})

        ranked.append(
            {
                "roster_id": roster_id,
                "team": (manager["team_name"] or manager["display_name"])
                if manager else f"Roster {roster_id}",
                "record": f"{entry['wins']}-{entry['losses']}"
                + (f"-{entry['ties']}" if entry["ties"] else ""),
                "wins": entry["wins"],
                "games": games,
                "points_for": round(entry["points_for"], 2),
                "points_against": round(entry["points_against"], 2),
                "avg_score": round(entry["points_for"] / len(entry["scores"]), 2),
                "consistency": round(_stdev(entry["scores"]), 2),
                "all_play": f"{entry['all_play_w']}-{entry['all_play_l']}",
                "all_play_pct": round(all_play_pct, 4),
                "expected_wins": expected_wins,
                # Positive means they have won more than their scoring deserved.
                "luck": round(entry["wins"] - expected_wins, 2),
                "optimal_points": round(entry["optimal"], 2),
                "lineup_efficiency": round(
                    entry["points_for"] / entry["optimal"], 4
                ) if entry["optimal"] else None,
                "points_left_on_bench": round(
                    entry["optimal"] - entry["points_for"], 2
                ),
                "lineup_value": value.get("lineup_value", 0),
            }
        )

    ranked.sort(key=lambda t: t["all_play_pct"], reverse=True)
    for position, team in enumerate(ranked, start=1):
        team["rank"] = position
        team["standings_rank"] = None
    by_record = sorted(
        ranked, key=lambda t: (t["wins"], t["points_for"]), reverse=True
    )
    for position, team in enumerate(by_record, start=1):
        team["standings_rank"] = position
        # Positive means the real standings flatter them: they sit higher in the
        # table than their scoring earned. Negative means the schedule has been
        # burying them.
        team["rank_gap"] = team["rank"] - team["standings_rank"]
    return ranked


def _optimal_by_week(conn, league_id, weeks, positions, slots) -> dict:
    """The most each roster could have scored each week, given its players."""
    rosters: dict[tuple, list] = {}
    for row in conn.execute(
        """SELECT week, roster_id, player_id, points FROM player_weeks
            WHERE league_id = ?""",
        (league_id,),
    ):
        rosters.setdefault((row["week"], row["roster_id"]), []).append(
            {"player_id": row["player_id"],
             "position": positions.get(row["player_id"], ""),
             "score": row["points"] or 0.0}
        )
    return {
        key: best_lineup(candidates, slots)[1]
        for key, candidates in rosters.items()
        if key[0] in set(weeks)
    }


def _stdev(values) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / (len(values) - 1)) ** 0.5


# -- single player ---------------------------------------------------------


def player_detail(conn, league_id: str, player_id: str) -> dict | None:
    """Everything known about one player, in this league's context.

    The value history is the reason this page exists. Market values are
    snapshotted daily and never overwritten, so a player accumulates a real
    price chart -- and that is what makes it possible to say a trade was fair
    *at the time* rather than only in hindsight. Early on the series is two or
    three points long, which the page says plainly instead of drawing a
    confident-looking line through almost nothing.
    """
    player = conn.execute(
        "SELECT * FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if not player:
        return None

    league = league_row(conn, league_id)
    config_key = config_key_for(league)

    history = [
        {"date": row["asof_date"], "value": row["value"],
         "overall_rank": row["overall_rank"], "position_rank": row["position_rank"]}
        for row in conn.execute(
            """SELECT asof_date, value, overall_rank, position_rank
                 FROM player_values
                WHERE config_key = ? AND player_id = ?
                ORDER BY asof_date""",
            (config_key, player_id),
        )
    ]

    weeks = [
        {"week": row["week"], "points": row["points"] or 0.0,
         "started": bool(row["started"]), "roster_id": row["roster_id"]}
        for row in conn.execute(
            """SELECT week, points, started, roster_id FROM player_weeks
                WHERE league_id = ? AND player_id = ? ORDER BY week""",
            (league_id, player_id),
        )
    ]

    adp = conn.execute(
        """SELECT adp, position_adp FROM player_adp
            WHERE player_id = ? ORDER BY asof_date DESC LIMIT 1""",
        (player_id,),
    ).fetchone()
    drafted = conn.execute(
        """SELECT pick_no, round, roster_id FROM draft_picks
            WHERE league_id = ? AND player_id = ?""",
        (league_id, player_id),
    ).fetchone()

    managers = {
        m["roster_id"]: (m["team_name"] or m["display_name"])
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }
    stints = [
        {**s, "team": managers.get(s["roster_id"], f"Roster {s['roster_id']}")}
        for s in roster_stints(conn, league_id)
        if s["player_id"] == player_id
    ]

    trades = []
    for row in conn.execute(
        """SELECT t.txn_id, t.week, tp.direction, tp.roster_id
             FROM transaction_players tp
             JOIN transactions t ON t.txn_id = tp.txn_id
            WHERE t.league_id = ? AND tp.player_id = ? AND t.type = 'trade'
            ORDER BY t.created_ms""",
        (league_id, player_id),
    ):
        trades.append(
            {"txn_id": row["txn_id"], "week": row["week"],
             "direction": row["direction"],
             "team": managers.get(row["roster_id"], "?")}
        )

    latest = history[-1] if history else {}
    first = history[0] if history else {}
    started = [w for w in weeks if w["started"]]
    return {
        "player_id": player_id,
        "name": player["name"],
        "position": player["position"],
        "team": player["team"],
        "age": player["age"],
        "status": player["status"],
        "value": latest.get("value"),
        "overall_rank": latest.get("overall_rank"),
        "position_rank": latest.get("position_rank"),
        "history": history,
        "value_change": (latest.get("value") - first.get("value"))
        if len(history) > 1 else None,
        "weeks": weeks,
        "points_started": round(sum(w["points"] for w in started), 2),
        "points_benched": round(
            sum(w["points"] for w in weeks if not w["started"]), 2
        ),
        "starts": len(started),
        "adp": adp["adp"] if adp else None,
        "position_adp": adp["position_adp"] if adp else None,
        "drafted_pick": drafted["pick_no"] if drafted else None,
        "drafted_by": managers.get(drafted["roster_id"]) if drafted else None,
        "adp_delta": round(drafted["pick_no"] - adp["adp"], 1)
        if drafted and adp and adp["adp"] else None,
        "stints": stints,
        "trades": trades,
        "owner": stints[-1]["team"] if stints else None,
    }


def chart_geometry(series: list[dict], x_key: str, y_key: str,
                   width: int = 560, height: int = 130, pad: int = 26) -> dict:
    """Pre-compute SVG coordinates for a small inline chart.

    Done in Python rather than in the template because Jinja arithmetic for
    axis scaling is unreadable and easy to get subtly wrong. Returns points,
    a path, and the axis extents the template needs to label.

    The y-axis starts at zero for counts and points, but for market value it is
    zoomed to the data range -- a player moving 8,200 to 8,300 is invisible on a
    zero-based axis, and value has no meaningful zero to anchor to.
    """
    if not series:
        return {"points": [], "path": "", "empty": True}

    values = [row[y_key] or 0 for row in series]
    low, high = min(values), max(values)
    if high == low:
        low, high = low - 1, high + 1
    span = high - low

    inner_w = width - pad * 2
    inner_h = height - pad * 2
    step = inner_w / max(len(series) - 1, 1)

    points = []
    for index, row in enumerate(series):
        value = row[y_key] or 0
        points.append(
            {
                "x": round(pad + index * step, 2),
                "y": round(pad + inner_h - ((value - low) / span) * inner_h, 2),
                "label": row[x_key],
                "value": value,
            }
        )
    return {
        "points": points,
        "path": "M " + " L ".join(f"{p['x']},{p['y']}" for p in points),
        "low": low,
        "high": high,
        "width": width,
        "height": height,
        "pad": pad,
        "empty": False,
    }


def bar_geometry(series: list[dict], x_key: str, y_key: str,
                 width: int = 560, height: int = 130, pad: int = 26) -> dict:
    """Bars for weekly scoring. Zero-based, because points have a real zero."""
    if not series:
        return {"bars": [], "empty": True}
    high = max((row[y_key] or 0) for row in series) or 1
    inner_w = width - pad * 2
    inner_h = height - pad * 2
    # A 2px surface gap between adjacent bars, per the mark spec.
    slot = inner_w / len(series)
    bar_w = max(slot - 2, 2)

    bars = []
    for index, row in enumerate(series):
        value = row[y_key] or 0
        bar_h = (value / high) * inner_h
        bars.append(
            {
                "x": round(pad + index * slot, 2),
                "y": round(pad + inner_h - bar_h, 2),
                "w": round(bar_w, 2),
                "h": round(max(bar_h, 1), 2),
                "label": row[x_key],
                "value": value,
                "muted": not row.get("started", True),
            }
        )
    return {"bars": bars, "high": high, "width": width, "height": height,
            "pad": pad, "empty": False}
