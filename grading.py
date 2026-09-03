"""Instant ("ex-ante") trade grading.

The question this answers is *what did each side give up, measured at the moment
of the trade* -- not who ended up winning it, which needs games to be played and
is phase 3's job.

Two numbers per side, kept deliberately separate rather than blended into one
composite:

  * **Value** -- market value received minus market value given. This is what
    the trade market says the pieces are worth.
  * **Fit** -- how much the team's best startable lineup improves. This is what
    the trade does for them *this week*.

They are reported separately because in a dynasty league they legitimately point
in opposite directions, and a single blended number would hide exactly the case
that is most interesting. A rebuilding team trading a 29-year-old star for two
first-round picks should *lose* fit and *gain* value, and that is a good trade,
not a C. Collapsing both into one letter would call it a wash and say nothing.

Fit also does the work that trade graders usually fake with a "consolidation
premium". Two bench players contribute nothing to a starting lineup, so a
2-for-1 that turns depth into a starter shows up as a fit gain automatically --
no invented multiplier required.
"""

from __future__ import annotations

import json

import analytics

# Letter grades from the share of value a side gained or lost, measured against
# the larger side of the deal. A 10% edge on a big trade is a real win; the same
# raw points on a swap of two backups is noise, which is why this is a ratio.
# How much a team's record counts toward projecting where their pick lands,
# once there is enough of a season to read. Record outweighs roster value
# because record is what actually sets the draft order; roster value is the
# corrective for a good team off to a bad start.
RECORD_WEIGHT = 0.70
RECORD_RAMP_GAMES = 6

GRADE_SCALE = [
    (0.25, "A+"), (0.15, "A"), (0.09, "A-"),
    (0.05, "B+"), (0.02, "B"), (-0.02, "B-"),
    (-0.05, "C+"), (-0.09, "C"), (-0.15, "C-"),
    (-0.25, "D"),
]


def letter(pct: float) -> str:
    for threshold, grade in GRADE_SCALE:
        if pct >= threshold:
            return grade
    return "F"


# -- asset valuation -------------------------------------------------------


def player_value(conn, config_key: str, asof: str, player_id: str) -> int:
    row = conn.execute(
        """SELECT value FROM player_values
            WHERE config_key = ? AND asof_date = ? AND player_id = ?""",
        (config_key, asof, player_id),
    ).fetchone()
    return row["value"] if row else 0


def pick_value(conn, league_id: str, config_key: str, asof: str,
               season: str, rnd: int, original_roster: int | None) -> tuple[int, str]:
    """Value a traded draft pick, and say how the number was arrived at.

    This is the awkward join in the whole project. Sleeper records a traded pick
    as season + round + whose pick it originally was, and never its slot --
    because the slot does not exist yet. FantasyCalc prices picks *by* slot
    ("2027 1st (Early)" is worth nearly double "2027 1st (Late)").

    So the slot has to be projected from how good the original owner is, worst
    team picking first. Three levels of confidence, best available first:

      1. an exact slot, when the draft order is already known;
      2. an early/mid/late tier, projected from the owner's standing;
      3. the round's blended value, when the market prices no tiers that far out.

    The method is returned alongside the number so the UI can show its work
    rather than presenting a projection as a fact.
    """
    labels = conn.execute(
        """SELECT slot, value FROM pick_values
            WHERE config_key = ? AND asof_date = ? AND season = ? AND round = ?""",
        (config_key, asof, str(season), rnd),
    ).fetchall()
    if not labels:
        return 0, "unpriced"

    by_slot = {row["slot"]: row["value"] for row in labels}
    tier = _project_pick_tier(conn, league_id, original_roster)

    if tier and tier.isdigit() and tier in by_slot:
        return by_slot[tier], f"slot {tier} (draft order known)"
    if tier in ("early", "mid", "late") and tier in by_slot:
        return by_slot[tier], f"projected {tier} (owner's standing)"
    if None in by_slot:
        return by_slot[None], "round average (no tier priced)"
    return max(by_slot.values()), "best available label"


def _project_pick_tier(conn, league_id: str, original_roster: int | None) -> str | None:
    """Guess whether a team's pick lands early, mid or late in its round.

    Worst team picks first, so a *bad* team's pick is an *early* -- and
    valuable -- pick. The question is what "bad" means, and two signals
    disagree: a team's record, and how good their roster actually is.

    Both are used, and **record is weighted more heavily**, because record is
    what literally determines draft order. Roster value is the corrective: a
    team sitting at 2-6 with the best roster in the league is far more likely
    to climb than one that is 2-6 on merit, so their pick should not be priced
    as a premium early selection.

    The weighting is not fixed. In week 1 a record carries no information at
    all -- everyone is 0-0 or 1-0 -- so the projection leans entirely on roster
    value and slides toward record as the sample grows, reaching its full
    weight around the six-game mark. That makes the preseason behaviour (pure
    roster value) a special case of the same formula rather than a separate
    branch.
    """
    if original_roster is None:
        return None
    ranked = analytics.power_rankings(conn, league_id)
    if not ranked:
        return None

    records = {
        m["roster_id"]: m
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }
    games = max(
        (m["wins"] or 0) + (m["losses"] or 0) + (m["ties"] or 0)
        for m in records.values()
    ) if records else 0

    # Record's weight ramps in over the first six games; before that the
    # sample is too small to say anything.
    record_weight = RECORD_WEIGHT * min(games / RECORD_RAMP_GAMES, 1.0) if games else 0.0
    value_weight = 1.0 - record_weight

    wins = {}
    for team in ranked:
        row = records.get(team["roster_id"])
        played = ((row["wins"] or 0) + (row["losses"] or 0) + (row["ties"] or 0)) if row else 0
        wins[team["roster_id"]] = ((row["wins"] or 0) / played) if row and played else 0.0

    scored = [
        {
            "roster_id": team["roster_id"],
            "score": value_weight * _normalise(team["lineup_value"], ranked, "lineup_value")
            + record_weight * _normalise_map(wins[team["roster_id"]], wins),
        }
        for team in ranked
    ]
    scored.sort(key=lambda t: t["score"])   # worst first, and worst picks first

    position = next(
        (i for i, t in enumerate(scored) if t["roster_id"] == original_roster), None
    )
    if position is None:
        return None
    third = max(len(scored) // 3, 1)
    return "early" if position < third else "late" if position >= 2 * third else "mid"


def _normalise(value, rows, key) -> float:
    values = [r[key] or 0 for r in rows]
    low, high = min(values), max(values)
    return ((value or 0) - low) / (high - low) if high > low else 0.5


def _normalise_map(value, mapping) -> float:
    values = list(mapping.values())
    low, high = min(values), max(values)
    return (value - low) / (high - low) if high > low else 0.5


def faab_dollar_value(conn, league_id: str, config_key: str, asof: str) -> float:
    """What one FAAB dollar is worth, in the same units as player value.

    Anchored on the idea that spending a full budget should buy roughly the
    weakest player anyone is actually starting: that is what the money is *for*.
    Deriving it from the league's own lineups keeps it honest across leagues
    with different budgets and different depth, rather than hardcoding a rate.
    """
    league = analytics.league_row(conn, league_id)
    budget = 100
    ranked = analytics.power_rankings(conn, league_id)
    startable = [
        p["score"] for team in ranked for p in team["lineup"] if p["score"] > 0
    ]
    if not startable:
        return 0.0
    return min(startable) / budget


# -- assembling a trade ----------------------------------------------------


def trade_sides(conn, txn_id: str) -> dict:
    """Normalize a completed Sleeper trade into {roster_id: {in, out}}.

    In a trade Sleeper records ``adds`` as {player: receiving roster} and
    ``drops`` as {player: sending roster}, so a side's incoming and outgoing
    players fall out of the direction column.
    """
    sides: dict[int, dict] = {}

    def side(rid):
        return sides.setdefault(
            rid, {"players_in": [], "players_out": [], "picks_in": [],
                  "picks_out": [], "faab_in": 0, "faab_out": 0}
        )

    for row in conn.execute(
        "SELECT player_id, roster_id, direction FROM transaction_players WHERE txn_id = ?",
        (txn_id,),
    ):
        key = "players_in" if row["direction"] == "add" else "players_out"
        side(row["roster_id"])[key].append(row["player_id"])

    for row in conn.execute(
        """SELECT season, round, original_roster, from_roster, to_roster
             FROM transaction_picks WHERE txn_id = ?""",
        (txn_id,),
    ):
        pick = (row["season"], row["round"], row["original_roster"])
        if row["to_roster"] is not None:
            side(row["to_roster"])["picks_in"].append(pick)
        if row["from_roster"] is not None:
            side(row["from_roster"])["picks_out"].append(pick)

    for row in conn.execute(
        "SELECT from_roster, to_roster, amount FROM transaction_faab WHERE txn_id = ?",
        (txn_id,),
    ):
        if row["to_roster"] is not None:
            side(row["to_roster"])["faab_in"] += row["amount"] or 0
        if row["from_roster"] is not None:
            side(row["from_roster"])["faab_out"] += row["amount"] or 0

    return sides


# -- the grade -------------------------------------------------------------


def grade(conn, league_id: str, sides: dict, asof: str | None = None,
          applied: bool = True) -> dict:
    """Grade an assembled trade. Works for a real trade or a hypothetical one.

    ``applied`` says whether the roster snapshot on file already reflects this
    trade. It does for a completed trade (Sleeper has since moved the players),
    and it does not for a proposal -- so the "before" and "after" lineups are
    built from opposite ends depending on which is being graded. Getting this
    backwards silently produces a fit delta of zero, which looks plausible.
    """
    league = analytics.league_row(conn, league_id)
    config_key = analytics.config_key_for(league)
    asof = asof or analytics.latest_value_date(conn, config_key)
    slots = analytics.starting_slots(json.loads(league["roster_positions"]))
    dollar = faab_dollar_value(conn, league_id, config_key, asof)

    managers = {
        m["roster_id"]: m
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }
    rosters = _current_rosters(conn, league_id, config_key, asof)

    graded = {}
    for roster_id, side in sides.items():
        assets_in = _price(conn, league_id, config_key, asof, dollar, side, "in")
        assets_out = _price(conn, league_id, config_key, asof, dollar, side, "out")
        value_in = sum(a["value"] for a in assets_in)
        value_out = sum(a["value"] for a in assets_out)
        delta = value_in - value_out
        scale = max(value_in, value_out, 1)

        before, after, lineup_delta = _fit(
            rosters.get(roster_id, []), side, slots, conn, config_key, asof, applied
        )
        # Fit is scored against the size of the deal, not the size of the
        # roster: the question is how much of the value you moved actually
        # landed in your starting lineup. Measuring it against total lineup
        # value instead would make every trade look like a rounding error.
        fit_pct = lineup_delta / scale

        manager = managers.get(roster_id)
        graded[roster_id] = {
            "roster_id": roster_id,
            "team": (manager["team_name"] or manager["display_name"])
            if manager else f"Roster {roster_id}",
            "assets_in": assets_in,
            "assets_out": assets_out,
            "value_in": value_in,
            "value_out": value_out,
            "value_delta": delta,
            "value_pct": round(delta / scale, 4),
            "value_grade": letter(delta / scale),
            "lineup_before": before,
            "lineup_after": after,
            "lineup_delta": round(lineup_delta, 1),
            "lineup_pct": round(fit_pct, 4),
            "fit_grade": letter(fit_pct),
            "age_in": _avg_age(conn, side.get("players_in", [])),
            "age_out": _avg_age(conn, side.get("players_out", [])),
        }

    for entry in graded.values():
        entry["verdict"] = _verdict(entry)
    return {"asof": asof, "sides": graded}


def _price(conn, league_id, config_key, asof, dollar, side, direction) -> list[dict]:
    """Turn one side's incoming or outgoing pieces into priced, labeled assets."""
    assets = []
    for pid in side.get(f"players_{direction}", []):
        row = conn.execute(
            "SELECT name, position, team, age FROM players WHERE player_id = ?", (pid,)
        ).fetchone()
        assets.append(
            {
                "kind": "player",
                "player_id": pid,
                "label": row["name"] if row else pid,
                "detail": f"{row['position']} {row['team']}" if row else "",
                "age": row["age"] if row else None,
                "value": player_value(conn, config_key, asof, pid),
                "basis": "market value",
            }
        )
    for season, rnd, original in side.get(f"picks_{direction}", []):
        value, basis = pick_value(
            conn, league_id, config_key, asof, season, rnd, original
        )
        assets.append(
            {
                "kind": "pick",
                "label": f"{season} round {rnd}",
                "detail": _ordinal_owner(conn, league_id, original),
                "age": None,
                "value": value,
                "basis": basis,
            }
        )
    faab = side.get(f"faab_{direction}", 0)
    if faab:
        assets.append(
            {
                "kind": "faab",
                "label": f"${faab} FAAB",
                "detail": "",
                "age": None,
                "value": int(round(faab * dollar)),
                "basis": "priced off the league's weakest starter",
            }
        )
    return assets


def _ordinal_owner(conn, league_id, original_roster) -> str:
    if original_roster is None:
        return ""
    row = conn.execute(
        "SELECT team_name, display_name FROM managers WHERE league_id = ? AND roster_id = ?",
        (league_id, original_roster),
    ).fetchone()
    if not row:
        return ""
    return f"via {row['team_name'] or row['display_name']}"


def _current_rosters(conn, league_id, config_key, asof) -> dict:
    """Latest roster snapshot per team, priced, active players only.

    Uses the most recent snapshot available. For a trade made before this
    project started collecting daily snapshots that is an approximation, and
    the fit number is reported as such -- but from here forward the right
    snapshot exists.
    """
    snapshot = conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM roster_slots WHERE league_id = ?",
        (league_id,),
    ).fetchone()["d"]
    out: dict[int, list] = {}
    for row in conn.execute(
        """SELECT rs.roster_id, rs.player_id, p.position, COALESCE(pv.value, 0) AS value
             FROM roster_slots rs
             JOIN players p ON p.player_id = rs.player_id
             LEFT JOIN player_values pv ON pv.player_id = rs.player_id
                   AND pv.config_key = ? AND pv.asof_date = ?
            WHERE rs.league_id = ? AND rs.snapshot_date = ? AND rs.slot = 'active'""",
        (config_key, asof, league_id, snapshot),
    ):
        out.setdefault(row["roster_id"], []).append(
            {"player_id": row["player_id"], "position": row["position"],
             "score": row["value"]}
        )
    return out


def _fit(roster, side, slots, conn, config_key, asof, applied: bool):
    """Best-lineup value before and after the trade, for one side.

    One of the two states is the roster on file; the other is reconstructed by
    moving the traded pieces across. Which is which depends on whether the
    trade has already happened -- see ``grade``.
    """
    known = {p["player_id"]: p for p in roster}
    other = dict(known)

    # Pieces to take out of the reconstructed side, and pieces to put into it.
    remove = side.get("players_in" if applied else "players_out", [])
    add = side.get("players_out" if applied else "players_in", [])

    for pid in remove:
        other.pop(pid, None)
    for pid in add:
        if pid not in other:
            other[pid] = _bench_entry(conn, config_key, asof, pid)

    _, known_value = analytics.best_lineup(list(known.values()), slots)
    _, other_value = analytics.best_lineup(list(other.values()), slots)

    before, after = (other_value, known_value) if applied else (known_value, other_value)
    return before, after, after - before


def _bench_entry(conn, config_key, asof, player_id) -> dict:
    row = conn.execute(
        "SELECT position FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    return {
        "player_id": player_id,
        "position": row["position"] if row else "",
        "score": player_value(conn, config_key, asof, player_id),
    }


def _avg_age(conn, player_ids) -> float | None:
    ages = [
        row["age"]
        for pid in player_ids
        for row in [conn.execute(
            "SELECT age FROM players WHERE player_id = ?", (pid,)
        ).fetchone()]
        if row and row["age"]
    ]
    return round(sum(ages) / len(ages), 1) if ages else None


def _verdict(side) -> str:
    """One plain sentence reading the two grades together."""
    value, fit = side["value_delta"], side["lineup_delta"]
    younger = (
        side["age_in"] is not None and side["age_out"] is not None
        and side["age_in"] < side["age_out"] - 0.5
    )
    if value > 0 and fit > 0:
        return "Won on value and got better right now — the rare clean win."
    if value > 0 and fit <= 0:
        base = "Gained market value but a weaker starting lineup"
        return base + (
            "; a rebuild trade, and a good one." if younger
            else " — worth it only if the pieces are for later."
        )
    if value <= 0 and fit > 0:
        return (
            "Paid above market to improve the lineup now — defensible for a "
            "contender, expensive for anyone else."
        )
    return "Lost value and did not improve the lineup."


# -- hypothetical trades ---------------------------------------------------


class ProposalError(ValueError):
    """A proposed trade names something that isn't on a roster in this league."""


def resolve_player(conn, league_id: str, query: str) -> tuple[str, int, str]:
    """Find a rostered player by (partial) name. Returns (id, roster_id, name).

    Restricted to players actually on a roster in this league, which is what
    makes a bare surname usually sufficient: there are ~250 rostered players,
    not 12,000. Ambiguity is an error rather than a guess, because silently
    grading the wrong Johnson is worse than asking again.
    """
    snapshot = conn.execute(
        "SELECT MAX(snapshot_date) AS d FROM roster_slots WHERE league_id = ?",
        (league_id,),
    ).fetchone()["d"]
    rows = conn.execute(
        """SELECT rs.player_id, rs.roster_id, p.name, p.position
             FROM roster_slots rs JOIN players p ON p.player_id = rs.player_id
            WHERE rs.league_id = ? AND rs.snapshot_date = ?
              AND LOWER(p.name) LIKE ?""",
        (league_id, snapshot, f"%{query.strip().lower()}%"),
    ).fetchall()

    if not rows:
        raise ProposalError(f"No rostered player matches {query!r}.")
    if len(rows) > 1:
        exact = [r for r in rows if r["name"].lower() == query.strip().lower()]
        if len(exact) == 1:
            rows = exact
        else:
            names = ", ".join(f"{r['name']} ({r['position']})" for r in rows[:6])
            raise ProposalError(f"{query!r} matches several players: {names}")
    return rows[0]["player_id"], rows[0]["roster_id"], rows[0]["name"]


def resolve_team(conn, league_id: str, query: str) -> int:
    rows = conn.execute(
        """SELECT roster_id, team_name, display_name FROM managers
            WHERE league_id = ?
              AND (LOWER(COALESCE(team_name,'')) LIKE ?
                   OR LOWER(COALESCE(display_name,'')) LIKE ?)""",
        (league_id, f"%{query.lower()}%", f"%{query.lower()}%"),
    ).fetchall()
    if not rows:
        raise ProposalError(f"No team matches {query!r}.")
    if len(rows) > 1:
        names = ", ".join(r["team_name"] or r["display_name"] for r in rows)
        raise ProposalError(f"{query!r} matches several teams: {names}")
    return rows[0]["roster_id"]


def build_proposal(conn, league_id: str, give: list[str], get: list[str],
                   from_team: str | None = None,
                   give_picks: list[str] | None = None,
                   get_picks: list[str] | None = None,
                   give_faab: int = 0, get_faab: int = 0,
                   to_team: str | None = None) -> dict:
    """Assemble a two-sided hypothetical from player names.

    Written from one manager's point of view -- what they give and what they get
    -- because that is how anyone actually thinks about a trade they are
    considering. The counterparty is inferred from whoever owns the incoming
    players, so it never has to be named.
    """
    given = [resolve_player(conn, league_id, name) for name in give]
    gotten = [resolve_player(conn, league_id, name) for name in get]

    give_rosters = {r for _, r, _ in given}
    get_rosters = {r for _, r, _ in gotten}
    if from_team:
        mine = resolve_team(conn, league_id, from_team)
    elif len(give_rosters) == 1:
        mine = give_rosters.pop()
    else:
        raise ProposalError(
            "Name your team with --from (needed when you are not giving up a "
            "player, or when the players span rosters)."
        )
    if to_team:
        theirs = resolve_team(conn, league_id, to_team)
    elif len(get_rosters) == 1:
        theirs = get_rosters.pop()
    else:
        raise ProposalError(
            "Name the other team with --to (needed for a picks-only trade, or "
            "when the players span rosters)."
        )
    if theirs == mine:
        raise ProposalError("Both sides of the trade are the same team.")

    # Hand off to the id-based builder so ownership checks and assembly live in
    # exactly one place.
    return sides_from_ids(
        conn, league_id, mine, theirs,
        [pid for pid, _, _ in given], [pid for pid, _, _ in gotten],
        give_picks=[_expand_pick(k, mine) for k in (give_picks or [])],
        get_picks=[_expand_pick(k, theirs) for k in (get_picks or [])],
        give_faab=give_faab, get_faab=get_faab,
    )


def _expand_pick(key: str, default_owner: int) -> str:
    """Allow ``2027:1`` to mean "that team's own pick" as a shorthand.

    Typing the original owner every time is tedious and almost always the team
    trading it, so the third field is optional on the command line.
    """
    parts = key.split(":")
    if len(parts) == 2:
        return f"{parts[0]}:{parts[1]}:{default_owner}"
    return key


def owned_picks(conn, league_id: str, roster_id: int) -> list[dict]:
    """Future draft picks a team currently holds, priced, soonest first.

    Includes picks acquired from other teams, which is why the original owner
    is carried through: a rebuilding team's future first is worth far more than
    a contender's, and the price depends on whose it is, not who holds it.
    """
    league = analytics.league_row(conn, league_id)
    config_key = analytics.config_key_for(league)
    asof = analytics.latest_value_date(conn, config_key)
    managers = {
        m["roster_id"]: (m["team_name"] or m["display_name"])
        for m in conn.execute(
            "SELECT * FROM managers WHERE league_id = ?", (league_id,)
        )
    }

    out = []
    for row in conn.execute(
        """SELECT season, round, original_roster FROM pick_ownership
            WHERE league_id = ? AND owner_roster = ?
            ORDER BY season, round, original_roster""",
        (league_id, roster_id),
    ):
        value, basis = pick_value(
            conn, league_id, config_key, asof,
            row["season"], row["round"], row["original_roster"],
        )
        own = row["original_roster"] == roster_id
        out.append(
            {
                "key": f"{row['season']}:{row['round']}:{row['original_roster']}",
                "season": row["season"],
                "round": row["round"],
                "original_roster": row["original_roster"],
                "label": f"{row['season']} round {row['round']}",
                "origin": "" if own else f"via {managers.get(row['original_roster'], '?')}",
                "value": value,
                "basis": basis,
            }
        )
    return out


def _parse_pick_key(key: str) -> tuple:
    season, rnd, original = key.split(":")
    return season, int(rnd), int(original)


def sides_from_ids(conn, league_id: str, mine: int, theirs: int,
                   give_ids: list[str], get_ids: list[str],
                   give_picks: list[str] | None = None,
                   get_picks: list[str] | None = None,
                   give_faab: int = 0, get_faab: int = 0) -> dict:
    """Build a two-sided proposal from player ids and explicit roster ids.

    The id-based twin of :func:`build_proposal`, for callers that already know
    exactly who they mean -- a web form, mainly, where the user picked from a
    list rather than typing a name.
    """
    give_picks = give_picks or []
    get_picks = get_picks or []
    if mine == theirs:
        raise ProposalError("Pick two different teams.")
    if not (give_ids or give_picks or give_faab):
        raise ProposalError("You have not offered anything.")
    if not (get_ids or get_picks or get_faab):
        raise ProposalError("You are not receiving anything.")

    owners = dict(
        conn.execute(
            """SELECT player_id, roster_id FROM roster_slots
                WHERE league_id = ? AND snapshot_date =
                      (SELECT MAX(snapshot_date) FROM roster_slots WHERE league_id = ?)""",
            (league_id, league_id),
        ).fetchall()
    )
    for pid in give_ids:
        if owners.get(pid) != mine:
            raise ProposalError("A player being given away is not on that roster.")
    for pid in get_ids:
        if owners.get(pid) != theirs:
            raise ProposalError("A player being acquired is not on that roster.")

    # Ownership is checked for picks the same way it is for players: a
    # proposal that moves an asset the team does not hold is a bug, not a trade.
    holders = {
        (row["season"], row["round"], row["original_roster"]): row["owner_roster"]
        for row in conn.execute(
            "SELECT * FROM pick_ownership WHERE league_id = ?", (league_id,)
        )
    }
    for keys, owner, who in ((give_picks, mine, "give away"),
                             (get_picks, theirs, "receive")):
        for key in keys:
            if holders.get(_parse_pick_key(key)) != owner:
                raise ProposalError(f"A pick you tried to {who} is not theirs to trade.")

    blank = lambda: {"players_in": [], "players_out": [], "picks_in": [],
                     "picks_out": [], "faab_in": 0, "faab_out": 0}
    sides = {mine: blank(), theirs: blank()}
    for pid in give_ids:
        sides[mine]["players_out"].append(pid)
        sides[theirs]["players_in"].append(pid)
    for pid in get_ids:
        sides[mine]["players_in"].append(pid)
        sides[theirs]["players_out"].append(pid)
    for key in give_picks:
        pick = _parse_pick_key(key)
        sides[mine]["picks_out"].append(pick)
        sides[theirs]["picks_in"].append(pick)
    for key in get_picks:
        pick = _parse_pick_key(key)
        sides[mine]["picks_in"].append(pick)
        sides[theirs]["picks_out"].append(pick)
    if give_faab:
        sides[mine]["faab_out"] = give_faab
        sides[theirs]["faab_in"] = give_faab
    if get_faab:
        sides[mine]["faab_in"] = get_faab
        sides[theirs]["faab_out"] = get_faab
    return sides


def roster_board(conn, league_id: str, roster_id: int) -> list[dict]:
    """Every active player on a roster, priced, best first — for a picker UI."""
    league = analytics.league_row(conn, league_id)
    config_key = analytics.config_key_for(league)
    asof = analytics.latest_value_date(conn, config_key)
    return [
        dict(row)
        for row in conn.execute(
            """SELECT rs.player_id, p.name, p.position, p.team,
                      COALESCE(pv.value, 0) AS value
                 FROM roster_slots rs
                 JOIN players p ON p.player_id = rs.player_id
                 LEFT JOIN player_values pv ON pv.player_id = rs.player_id
                       AND pv.config_key = ? AND pv.asof_date = ?
                WHERE rs.league_id = ? AND rs.roster_id = ? AND rs.slot = 'active'
                  AND rs.snapshot_date =
                      (SELECT MAX(snapshot_date) FROM roster_slots WHERE league_id = ?)
                ORDER BY value DESC, p.name""",
            (config_key, asof, league_id, roster_id, league_id),
        )
    ]


def completed_trades(conn, league_id: str) -> list[dict]:
    """Every completed trade in the league, graded."""
    out = []
    for row in conn.execute(
        """SELECT txn_id, week, created_ms FROM transactions
            WHERE league_id = ? AND type = 'trade' AND status = 'complete'
            ORDER BY created_ms DESC""",
        (league_id,),
    ):
        result = grade(conn, league_id, trade_sides(conn, row["txn_id"]))
        out.append({"txn_id": row["txn_id"], "week": row["week"],
                    "created_ms": row["created_ms"], **result})
    return out
