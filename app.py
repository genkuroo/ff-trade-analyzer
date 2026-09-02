"""ff-trade-analyzer web UI -- read-only view of ffta.db.

DB-only by design: this app never calls Sleeper or FantasyCalc. Fetching is the
scheduled sync's job (cli.py), so a page load can never be slow, rate-limited,
or dependent on someone else's API being up.

Run:  python app.py   ->  http://localhost:5003
"""

import os

from flask import Flask, abort, redirect, render_template, request, url_for

import analytics
import config
import db
import grading
import retro

app = Flask(__name__)


@app.route("/")
def index():
    """Send the visitor straight to a league -- the primary one if set."""
    conn = db.connect()
    leagues = analytics.all_leagues(conn)
    conn.close()
    if not leagues:
        return render_template("empty.html"), 503

    preferred = {entry["id"] for entry in _configured_ids()}
    target = next((l for l in leagues if l["league_id"] in preferred), leagues[0])
    return redirect(url_for("league", league_id=target["league_id"]))


def _configured_ids():
    try:
        return config.leagues()
    except config.ConfigError:
        return []


@app.route("/league/<league_id>")
def league(league_id):
    conn = db.connect()
    row = analytics.league_row(conn, league_id)
    if not row:
        conn.close()
        abort(404)
    context = {
        "summary": analytics.league_summary(conn, league_id),
        "rankings": analytics.power_rankings(conn, league_id),
        "transactions": analytics.recent_transactions(conn, league_id, limit=15),
        "leagues": analytics.all_leagues(conn),
        "league_id": league_id,
    }
    conn.close()
    return render_template("index.html", **context)


@app.route("/league/<league_id>/trades")
def trades(league_id):
    conn = db.connect()
    if not analytics.league_row(conn, league_id):
        conn.close()
        abort(404)
    trades = grading.completed_trades(conn, league_id)
    # The retrospective is a separate pass: it needs games to have been played,
    # and a league with no scoring yet should still show the instant grades.
    for trade in trades:
        trade["retro"] = retro.retrospective(conn, league_id, trade["txn_id"])
    context = {
        "summary": analytics.league_summary(conn, league_id),
        "trades": trades,
        "league_id": league_id,
    }
    conn.close()
    return render_template("trades.html", **context)


@app.route("/league/<league_id>/machine")
def machine(league_id):
    """The trade machine: pick players from two rosters, get both grades.

    Deliberately a GET with everything in the query string. Nothing is written,
    so a proposal is a plain URL — which means a graded trade can be pasted
    into the league chat and everyone sees the same thing.
    """
    conn = db.connect()
    if not analytics.league_row(conn, league_id):
        conn.close()
        abort(404)

    teams = analytics.power_rankings(conn, league_id)
    ids = [t["roster_id"] for t in teams]
    mine = request.args.get("a", type=int) or (ids[0] if ids else None)
    theirs = request.args.get("b", type=int) or (ids[1] if len(ids) > 1 else None)
    give = request.args.getlist("give")
    get = request.args.getlist("get")

    result, error = None, None
    if give and get:
        try:
            sides = grading.sides_from_ids(conn, league_id, mine, theirs, give, get)
            # applied=False: these rosters do not include the proposed trade.
            result = grading.grade(conn, league_id, sides, applied=False)
        except grading.ProposalError as exc:
            error = str(exc)

    context = {
        "summary": analytics.league_summary(conn, league_id),
        "league_id": league_id,
        "teams": teams,
        "mine": mine,
        "theirs": theirs,
        "board_mine": grading.roster_board(conn, league_id, mine) if mine else [],
        "board_theirs": grading.roster_board(conn, league_id, theirs) if theirs else [],
        "give": set(give),
        "get": set(get),
        "result": result,
        "error": error,
    }
    conn.close()
    return render_template("machine.html", **context)


@app.route("/league/<league_id>/players")
def players(league_id):
    """Raw market observations per player, next to the movement derived from them.

    Filtering is server-side (it decides which rows exist); sorting is
    client-side (it only reorders rows already sent). That split keeps a
    re-sort instant and avoids a round trip for something the browser can do.
    """
    conn = db.connect()
    if not analytics.league_row(conn, league_id):
        conn.close()
        abort(404)

    window = request.args.get("window", type=int) or 7
    position = request.args.get("pos") or ""
    owner = request.args.get("owner", type=int)
    scope = request.args.get("scope") or "rostered"

    # Built once against every valued player, then filtered in memory. The
    # draft report needs the unfiltered set anyway, so computing it twice would
    # pay for the same join twice.
    everyone = analytics.player_report(conn, league_id, window_days=window,
                                       rostered_only=False)
    draft = analytics.draft_report(conn, league_id, rows=everyone)

    rows = everyone if scope == "all" else [r for r in everyone if r["roster_id"]]
    if position:
        rows = [r for r in rows if r["position"] == position]
    if owner:
        rows = [r for r in rows if r.get("roster_id") == owner]

    context = {
        "summary": analytics.league_summary(conn, league_id),
        "league_id": league_id,
        "rows": rows,
        "draft": draft,
        "teams": analytics.power_rankings(conn, league_id),
        "window": window,
        "position": position,
        "owner": owner,
        "scope": scope,
        "positions": sorted({r["position"] for r in rows if r["position"]}),
    }
    conn.close()
    return render_template("players.html", **context)


@app.route("/league/<league_id>/team/<int:roster_id>")
def team(league_id, roster_id):
    """One team's roster, split into who is here now and who has passed through.

    The two are genuinely different questions. Power rankings care about the
    current roster; trade grading cares about who was held *when*, because a
    player's points only count for you while he was actually yours.
    """
    conn = db.connect()
    if not analytics.league_row(conn, league_id):
        conn.close()
        abort(404)
    ranked = analytics.power_rankings(conn, league_id)
    entry = next((t for t in ranked if t["roster_id"] == roster_id), None)
    if entry is None:
        conn.close()
        abort(404)
    context = {
        "summary": analytics.league_summary(conn, league_id),
        "league_id": league_id,
        "team": entry,
        "teams": ranked,
        "history": analytics.all_time_roster(conn, league_id, roster_id),
    }
    conn.close()
    return render_template("team.html", **context)


@app.route("/healthz")
def healthz():
    """Liveness probe that also proves the database is readable.

    A container that is up but pointed at an empty volume is a failure worth
    catching, so this checks for at least one synced league rather than just
    returning 200 unconditionally.
    """
    try:
        conn = db.connect()
        count = conn.execute("SELECT COUNT(*) AS n FROM leagues").fetchone()["n"]
        conn.close()
    except Exception as exc:  # noqa: BLE001 - the probe must never raise
        return {"ok": False, "error": str(exc)}, 503
    return ({"ok": True, "leagues": count}, 200) if count else (
        {"ok": False, "error": "no leagues synced"}, 503
    )


if __name__ == "__main__":
    db.init_db().close()
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 5003)), debug=True)
