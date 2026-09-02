"""Command line entry point for ff-trade-analyzer.

    python cli.py sync                 sync every configured league
    python cli.py sync --history       ...and every prior season of each
    python cli.py sync --no-values     league data only (the frequent poll)
    python cli.py values               snapshot market values only (daily)
    python cli.py discover <username>  list a Sleeper user's leagues + ids
    python cli.py status               what is in the database right now

Leagues come from FFTA_LEAGUES or config.json -- see config.py.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

import config
import db
import ingest
from sources import sleeper


def _leagues():
    try:
        return config.leagues()
    except config.ConfigError as exc:
        sys.exit(str(exc))


def cmd_sync(args) -> None:
    entries = _leagues()
    conn = db.init_db()

    # The catalog is disk-cached for a day, so the frequent poll can ask for it
    # every time and only actually fetch once.
    if not args.no_players:
        print(f"player catalog: {ingest.sync_players(conn):,} players")

    for entry in entries:
        print(f"\nsyncing {entry['label']} ({entry['id']})...")
        if args.history:
            summaries = ingest.sync_history(conn, entry["id"])
        else:
            summaries = [
                ingest.sync_league(conn, entry["id"], with_values=not args.no_values)
            ]
        for summary in summaries:
            _print_summary(summary)
    conn.close()


def cmd_values(args) -> None:
    """Snapshot market values without touching league data.

    Split out from `sync` so the two can run on different schedules: leagues
    every few minutes (to catch a trade), values once a day (because that is
    how often they meaningfully move).
    """
    entries = _leagues()
    conn = db.init_db()
    shapes = []
    for entry in entries:
        shapes.append(ingest.league_shape(sleeper.league(entry["id"])))
    total = ingest.sync_all_values(conn, shapes)
    print(f"snapshotted {total} values across {len(shapes)} league(s)")
    conn.close()


def _print_summary(summary: dict) -> None:
    line = (
        f"  {summary['league']}: weeks 1-{summary['through_week']}, "
        f"{summary['managers']} managers, {summary['transactions']} transactions, "
        f"{summary['player_weeks']:,} player-weeks, "
        f"{summary['draft_picks']} draft picks"
    )
    if summary.get("values"):
        players, picks = summary["values"]
        line += f", {players} valued players + {picks} picks"
    print(line)


def cmd_discover(args) -> None:
    for league in sleeper.user_leagues(args.username, args.season):
        settings = league.get("settings") or {}
        kind = {0: "redraft", 1: "keeper", 2: "dynasty"}.get(settings.get("type"), "?")
        print(
            f"{league['league_id']}  {league['name']:<32} "
            f"{league.get('season')}  {kind:<8} {league.get('total_rosters')} teams"
        )


def cmd_status(args) -> None:
    conn = db.init_db()
    rows = conn.execute(
        """SELECT l.league_id, l.name, l.season, l.status,
                  (SELECT COUNT(*) FROM transactions t
                    WHERE t.league_id = l.league_id AND t.type = 'trade') AS trades,
                  (SELECT COUNT(*) FROM transactions t
                    WHERE t.league_id = l.league_id) AS txns,
                  (SELECT COUNT(DISTINCT week) FROM player_weeks pw
                    WHERE pw.league_id = l.league_id) AS weeks
             FROM leagues l ORDER BY l.season DESC, l.name"""
    ).fetchall()
    if not rows:
        print("no leagues synced yet -- run: python cli.py sync")
        return
    for row in rows:
        print(
            f"{row['name']} ({row['season']}, {row['status']}): "
            f"{row['trades']} trades / {row['txns']} transactions, "
            f"{row['weeks']} weeks of scoring"
        )
    counts = conn.execute(
        """SELECT (SELECT COUNT(*) FROM players) AS players,
                  (SELECT COUNT(DISTINCT asof_date) FROM player_values) AS snapshots,
                  (SELECT COUNT(*) FROM player_weeks) AS player_weeks"""
    ).fetchone()
    print(
        f"\ncatalog: {counts['players']:,} players | "
        f"{counts['snapshots']} value snapshot(s) | "
        f"{counts['player_weeks']:,} player-weeks"
    )
    conn.close()


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(message)s")
    parser = argparse.ArgumentParser(prog="ff-trade-analyzer")
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="pull leagues from Sleeper into the database")
    p_sync.add_argument(
        "--history", action="store_true",
        help="also walk previous_league_id back through prior seasons",
    )
    p_sync.add_argument(
        "--no-values", action="store_true",
        help="skip the market-value snapshot (use for the frequent poll)",
    )
    p_sync.add_argument(
        "--no-players", action="store_true",
        help="skip refreshing the NFL player catalog",
    )
    p_sync.set_defaults(func=cmd_sync)

    sub.add_parser("values", help="snapshot market values only").set_defaults(
        func=cmd_values
    )

    p_disc = sub.add_parser("discover", help="list a Sleeper user's leagues and ids")
    p_disc.add_argument("username")
    p_disc.add_argument("--season", default="2026")
    p_disc.set_defaults(func=cmd_discover)

    sub.add_parser("status", help="summarize what is in the database").set_defaults(
        func=cmd_status
    )

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
