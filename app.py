"""ff-trade-analyzer web UI -- read-only view of ffta.db.

DB-only by design: this app never calls Sleeper or FantasyCalc. Fetching is the
scheduled sync's job (cli.py), so a page load can never be slow, rate-limited,
or dependent on someone else's API being up.

Run:  python app.py   ->  http://localhost:5003
"""

import os

from flask import Flask, abort, redirect, render_template, url_for

import analytics
import config
import db

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
