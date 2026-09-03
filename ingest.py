"""Pull a Sleeper league (and the market values it should be graded against)
into the local database.

Everything here is idempotent: each write is an INSERT OR REPLACE keyed on the
natural id, so re-running a sync mid-season updates what changed and leaves the
rest alone. That matters because this runs on a schedule -- a broken week
should be fixable by just syncing again.

The one deliberately non-idempotent thing is value snapshots. Those are keyed
by date, so running twice in a day overwrites the day's snapshot while running
on a new day appends. Building up that history is what later lets a trade be
graded against the values that were true when it was made.
"""

from __future__ import annotations

import datetime as dt
import json
import logging

import db
from sources import sleeper, values

log = logging.getLogger(__name__)

# Weeks past the fantasy playoffs never have fantasy-relevant data.
LAST_NFL_WEEK = 18

# How many future rookie drafts to treat as tradeable. Dynasty leagues rarely
# trade further out than this, and the market stops pricing picks beyond it.
FUTURE_PICK_SEASONS = 3


def today() -> str:
    return dt.date.today().isoformat()


# -- global reference ------------------------------------------------------


def sync_players(conn) -> int:
    """Refresh the shared NFL player catalog. Cached to disk for a day."""
    catalog = sleeper.players()
    conn.executemany(
        """INSERT OR REPLACE INTO players
           (player_id, name, position, team, age, status)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [
            (pid, p["name"], p["position"], p["team"], p.get("age"), p.get("status"))
            for pid, p in catalog.items()
        ],
    )
    conn.commit()
    return len(catalog)


def sync_adp(conn, season: str) -> int:
    """Snapshot today's average draft position for every drafted player."""
    rows = sleeper.adp(season)
    asof = today()
    conn.executemany(
        """INSERT OR REPLACE INTO player_adp
           (asof_date, season, player_id, adp, position_adp)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (asof, season, pid, row["adp"], row["position_adp"])
            for pid, row in rows.items()
        ],
    )
    conn.commit()
    log.info("adp %s: %d players", season, len(rows))
    return len(rows)


def sync_values(conn, is_dynasty: bool, num_qbs: int, num_teams: int, ppr: float) -> tuple[int, int]:
    """Snapshot today's market values for one league shape."""
    key = values.config_key(is_dynasty, num_qbs, num_teams, ppr)
    players, picks = values.split(values.fetch(is_dynasty, num_qbs, num_teams, ppr))
    asof = today()

    # Sleeper's own ordering, joined in as an independent second opinion on
    # where a player belongs -- two disagreeing sources are more informative
    # than one confident one.
    search_ranks = {
        pid: row.get("search_rank")
        for pid, row in (sleeper.players() or {}).items()
    }
    conn.executemany(
        """INSERT OR REPLACE INTO player_values
           (asof_date, config_key, player_id, value, redraft_value,
            combined_value, overall_rank, position_rank, trend_30day, tier,
            is_starter, trade_frequency, roster_percent, value_stddev_pct,
            search_rank)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                asof, key, p["player_id"], p["value"], p["redraft_value"],
                p["combined_value"], p["overall_rank"], p["position_rank"],
                p["trend_30day"], p["tier"], int(bool(p["is_starter"])),
                p["trade_frequency"], p["roster_percent"], p["value_stddev_pct"],
                search_ranks.get(p["player_id"]),
            )
            for p in players
        ],
    )
    conn.executemany(
        """INSERT OR REPLACE INTO pick_values
           (asof_date, config_key, label, season, round, slot, value)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (asof, key, p["label"], p["season"], p["round"], p["slot"], p["value"])
            for p in picks
        ],
    )
    conn.commit()
    log.info("values %s: %d players, %d picks", key, len(players), len(picks))
    return len(players), len(picks)


# -- one league ------------------------------------------------------------


def league_shape(meta: dict) -> dict:
    """Derive the valuation-relevant shape of a league from its settings.

    ``num_qbs`` counts superflex slots as a QB slot, because that is what
    doubles quarterback value and it is the single biggest lever on a trade
    grade in leagues that have it.
    """
    positions = meta.get("roster_positions") or []
    num_qbs = positions.count("QB") + positions.count("SUPER_FLEX")
    return {
        "is_dynasty": (meta.get("settings") or {}).get("type") == 2,
        "num_qbs": max(num_qbs, 1),
        "num_teams": meta.get("total_rosters") or 12,
        "ppr": float((meta.get("scoring_settings") or {}).get("rec") or 0.0),
    }


def sync_league(
    conn, league_id: str, through_week: int | None = None, with_values: bool = True
) -> dict:
    """Sync one league end to end. Returns a summary of what was written.

    ``with_values=False`` skips the market-value snapshot. The frequent
    scheduled sync on the Pi uses that: league data is worth re-checking every
    few minutes for new trades, but FantasyCalc values move on the order of a
    day and re-snapshotting them on every poll would just overwrite the day's
    snapshot with a near-identical one.
    """
    meta = sleeper.league(league_id)
    settings = meta.get("settings") or {}
    shape = league_shape(meta)

    conn.execute(
        """INSERT OR REPLACE INTO leagues
           (league_id, name, season, status, league_type, num_teams,
            roster_positions, playoff_week_start, trade_deadline, ppr, num_qbs,
            previous_league_id, avatar_id, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            league_id, meta.get("name"), meta.get("season"), meta.get("status"),
            settings.get("type"), meta.get("total_rosters"),
            json.dumps(meta.get("roster_positions") or []),
            settings.get("playoff_week_start"), settings.get("trade_deadline"),
            shape["ppr"], shape["num_qbs"], meta.get("previous_league_id"),
            meta.get("avatar"),
            dt.datetime.now().isoformat(timespec="seconds"),
        ),
    )

    counts = {"league": meta.get("name")}
    counts["managers"] = _sync_managers(conn, league_id)
    counts["roster_slots"] = _sync_rosters(conn, league_id)

    # A finished season is worth syncing all the way out; a live one only up to
    # the current week, since later weeks are guaranteed empty.
    if through_week is None:
        if meta.get("status") == "complete":
            through_week = LAST_NFL_WEEK
        else:
            through_week = max(int(sleeper.state().get("week") or 1), 1)

    counts["transactions"] = _sync_transactions(conn, league_id, through_week)
    counts["player_weeks"] = _sync_matchups(conn, league_id, through_week)
    counts["draft_picks"] = _sync_drafts(conn, league_id)
    counts["pick_ownership"] = _sync_pick_ownership(conn, league_id, meta)
    counts["adp"] = sync_adp(conn, meta.get("season") or "")
    conn.commit()

    counts["values"] = sync_values(conn, **shape) if with_values else None
    counts["through_week"] = through_week
    counts["shape"] = shape
    return counts


def _sync_managers(conn, league_id: str) -> int:
    # Identity lives on /users and the team slot lives on /rosters; joining
    # them here means nothing downstream has to know they are separate.
    by_user = {u["user_id"]: u for u in sleeper.users(league_id)}
    rows = []
    for roster in sleeper.rosters(league_id):
        user = by_user.get(roster.get("owner_id")) or {}
        metadata = user.get("metadata") or {}
        record = roster.get("settings") or {}
        rows.append(
            (
                league_id, roster["roster_id"], roster.get("owner_id"),
                user.get("display_name"),
                metadata.get("team_name") or user.get("display_name"),
                # A manager can set a custom avatar for this team specifically,
                # separate from their personal one; that choice wins when made.
                metadata.get("avatar") or user.get("avatar"),
                record.get("wins"), record.get("losses"), record.get("ties"),
                # Sleeper splits points into whole and decimal parts.
                float(f"{record.get('fpts', 0)}.{record.get('fpts_decimal', 0)}"),
            )
        )
    conn.executemany(
        """INSERT OR REPLACE INTO managers
           (league_id, roster_id, user_id, display_name, team_name, avatar_id,
            wins, losses, ties, points_for)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


def _sync_rosters(conn, league_id: str) -> int:
    """Snapshot who is on every roster today, tagging taxi and IR separately.

    Taxi and reserve players cannot be started, so a grader that counted them
    as ordinary depth would overrate rebuilding teams.
    """
    snapshot = today()
    rows = []
    for roster in sleeper.rosters(league_id):
        rid = roster["roster_id"]
        taxi = set(roster.get("taxi") or [])
        reserve = set(roster.get("reserve") or [])
        for pid in roster.get("players") or []:
            slot = "taxi" if pid in taxi else "reserve" if pid in reserve else "active"
            rows.append((league_id, snapshot, rid, pid, slot))
    conn.executemany(
        """INSERT OR REPLACE INTO roster_slots
           (league_id, snapshot_date, roster_id, player_id, slot)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )
    return len(rows)


def _sync_transactions(conn, league_id: str, through_week: int) -> int:
    total = 0
    for week in range(1, through_week + 1):
        for txn in sleeper.transactions(league_id, week):
            txn_id = txn["transaction_id"]
            conn.execute(
                """INSERT OR REPLACE INTO transactions
                   (txn_id, league_id, week, type, status, created_ms, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    txn_id, league_id, txn.get("leg") or week, txn.get("type"),
                    txn.get("status"), txn.get("created"), json.dumps(txn),
                ),
            )
            # adds/drops are {player_id: roster_id}; flattening them is what
            # makes "what did roster 4 receive in this trade" a simple query.
            for direction in ("adds", "drops"):
                for pid, rid in (txn.get(direction) or {}).items():
                    conn.execute(
                        """INSERT OR REPLACE INTO transaction_players
                           (txn_id, league_id, player_id, roster_id, direction)
                           VALUES (?, ?, ?, ?, ?)""",
                        (txn_id, league_id, pid, rid, direction[:-1]),
                    )
            for pick in txn.get("draft_picks") or []:
                conn.execute(
                    """INSERT OR REPLACE INTO transaction_picks
                       (txn_id, league_id, season, round, original_roster,
                        from_roster, to_roster)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        txn_id, league_id, pick.get("season"), pick.get("round"),
                        pick.get("roster_id"), pick.get("previous_owner_id"),
                        pick.get("owner_id"),
                    ),
                )
            for move in txn.get("waiver_budget") or []:
                conn.execute(
                    """INSERT OR REPLACE INTO transaction_faab
                       (txn_id, league_id, from_roster, to_roster, amount)
                       VALUES (?, ?, ?, ?, ?)""",
                    (txn_id, league_id, move.get("sender"), move.get("receiver"),
                     move.get("amount")),
                )
            total += 1
    return total


def _sync_matchups(conn, league_id: str, through_week: int) -> int:
    """Flatten every week's matchups into one row per player per week.

    Sleeper returns ``starters`` (an ordered array of player ids) alongside
    ``players_points`` (a map of every rostered player's score). Recording the
    two together, with a ``started`` flag, is what later lets the grader
    distinguish points a manager actually banked from points that sat on their
    bench -- the distinction most trade graders quietly skip.
    """
    total = 0
    for week in range(1, through_week + 1):
        for team in sleeper.matchups(league_id, week):
            rid = team.get("roster_id")
            if rid is None:
                continue
            conn.execute(
                """INSERT OR REPLACE INTO team_weeks
                   (league_id, week, roster_id, matchup_id, points)
                   VALUES (?, ?, ?, ?, ?)""",
                (league_id, week, rid, team.get("matchup_id"), team.get("points")),
            )
            started = set(team.get("starters") or [])
            for pid, points in (team.get("players_points") or {}).items():
                conn.execute(
                    """INSERT OR REPLACE INTO player_weeks
                       (league_id, week, roster_id, player_id, points, started)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (league_id, week, rid, pid, points, int(pid in started)),
                )
                total += 1
    return total


def _sync_drafts(conn, league_id: str) -> int:
    """Record actual draft selections.

    This is how a traded pick eventually earns a retrospective grade: the pick
    itself is an abstraction until someone uses it, and then it simply *is* the
    player that was taken with it.
    """
    total = 0
    for draft in sleeper.drafts(league_id):
        draft_id = draft["draft_id"]
        slot_to_roster = {
            int(slot): rid
            for slot, rid in (draft.get("slot_to_roster_id") or {}).items()
        }
        for pick in sleeper.draft_picks(draft_id):
            conn.execute(
                """INSERT OR REPLACE INTO draft_picks
                   (league_id, draft_id, season, round, pick_no, roster_id, player_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    league_id, draft_id, draft.get("season"), pick.get("round"),
                    pick.get("pick_no"),
                    pick.get("roster_id") or slot_to_roster.get(pick.get("draft_slot")),
                    pick.get("player_id"),
                ),
            )
            total += 1
    return total


def sync_all_values(conn, shapes: list[dict]) -> int:
    """Snapshot values once per distinct league shape.

    Ten leagues that are all ten-team 1QB PPR dynasty cost one fetch, not ten.
    """
    seen, total = set(), 0
    for shape in shapes:
        key = values.config_key(**shape)
        if key in seen:
            continue
        seen.add(key)
        players, picks = sync_values(conn, **shape)
        total += players + picks
    return total


def _sync_pick_ownership(conn, league_id: str, meta: dict) -> int:
    """Work out who holds every future draft pick.

    Sleeper's ``/traded_picks`` lists only picks that have moved, so ownership
    has to be built the other way round: assume every team still holds its own
    picks, then apply the transfers on top. Doing it here rather than at query
    time means a trade proposal can simply ask what a team owns.

    Only future seasons are generated. A pick in a draft that has already
    happened is not an asset any more -- it is a player.
    """
    settings = meta.get("settings") or {}
    rounds = settings.get("draft_rounds") or 4
    season = int(meta.get("season") or 0)
    teams = meta.get("total_rosters") or 12
    if not season:
        return 0

    conn.execute("DELETE FROM pick_ownership WHERE league_id = ?", (league_id,))
    rows = [
        (league_id, str(year), rnd, roster, roster)
        for year in range(season + 1, season + 1 + FUTURE_PICK_SEASONS)
        for rnd in range(1, rounds + 1)
        for roster in range(1, teams + 1)
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO pick_ownership
           (league_id, season, round, original_roster, owner_roster)
           VALUES (?, ?, ?, ?, ?)""",
        rows,
    )

    moved = 0
    for pick in sleeper.traded_picks(league_id):
        if pick.get("owner_id") is None:
            continue
        conn.execute(
            """UPDATE pick_ownership SET owner_roster = ?
                WHERE league_id = ? AND season = ? AND round = ?
                  AND original_roster = ?""",
            (pick["owner_id"], league_id, str(pick.get("season")),
             pick.get("round"), pick.get("roster_id")),
        )
        moved += conn.total_changes and 1 or 0
    log.info("pick ownership: %d picks, %d have been traded", len(rows), moved)
    return len(rows)


def sync_history(conn, league_id: str, max_seasons: int = 10) -> list:
    """Walk a dynasty league backwards through previous_league_id and sync each.

    Sleeper models each season of a dynasty league as its own league id, so the
    only way to get trade history older than this year is to follow that chain.
    """
    synced = []
    current = league_id
    while current and len(synced) < max_seasons:
        # Only the newest season needs a fresh value snapshot -- prior seasons
        # share the same shape, so the fetch would be identical.
        summary = sync_league(conn, current, with_values=not synced)
        synced.append(summary)
        row = conn.execute(
            "SELECT previous_league_id FROM leagues WHERE league_id = ?", (current,)
        ).fetchone()
        current = row["previous_league_id"] if row else None
    return synced
