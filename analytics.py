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
    """
    league = league_row(conn, league_id)
    if not league:
        return []
    key = config_key_for(league)
    asof = latest_value_date(conn, key)
    slots = starting_slots(json.loads(league["roster_positions"]))
    snapshot = _latest_roster_date(conn, league_id)

    rows = conn.execute(
        """SELECT rs.roster_id, rs.player_id, rs.slot AS roster_slot,
                  p.name, p.position, p.team, p.age,
                  COALESCE(pv.value, 0) AS value
             FROM roster_slots rs
             JOIN players p ON p.player_id = rs.player_id
             LEFT JOIN player_values pv
                    ON pv.player_id = rs.player_id
                   AND pv.config_key = ? AND pv.asof_date = ?
            WHERE rs.league_id = ? AND rs.snapshot_date = ?""",
        (key, asof, league_id, snapshot),
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
        ranked.append(
            {
                "roster_id": roster_id,
                "team": (manager["team_name"] if manager else None)
                or (manager["display_name"] if manager else None)
                or f"Roster {roster_id}",
                "manager": manager["display_name"] if manager else "",
                "record": _record(manager),
                "points_for": manager["points_for"] if manager else 0.0,
                "lineup_value": lineup_value,
                "total_value": sum(p["value"] for p in players),
                "lineup": lineup,
                "best_player": max(players, key=lambda p: p["value"])["name"]
                if players else "",
                "depth_ratio": round(
                    (sum(p["value"] for p in players) or 1) / (lineup_value or 1), 2
                ),
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
