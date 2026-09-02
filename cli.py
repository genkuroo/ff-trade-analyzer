"""Command line entry point for ff-trade-analyzer.

    python cli.py sync                 sync every configured league
    python cli.py sync --history       ...and every prior season of each
    python cli.py sync --no-values     league data only (the frequent poll)
    python cli.py values               snapshot market values only (daily)
    python cli.py power                luck-adjusted power rankings
    python cli.py trades               grade every completed trade
    python cli.py propose --give X --get Y   grade a hypothetical trade
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

import analytics
import config
import db
import grading
import ingest
import retro
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


def _only_league(conn, requested: str | None) -> str:
    """Pick the league to operate on: the one named, or the only one synced."""
    leagues = analytics.all_leagues(conn)
    if not leagues:
        sys.exit("No leagues synced yet — run: python cli.py sync")
    if requested:
        for league in leagues:
            if requested in (league["league_id"], league["name"]):
                return league["league_id"]
        sys.exit(f"No synced league matches {requested!r}.")
    if len(leagues) > 1:
        names = ", ".join(f"{l['name']} ({l['league_id']})" for l in leagues)
        sys.exit(f"Several leagues synced — pass --league. One of: {names}")
    return leagues[0]["league_id"]


def _print_grade(result: dict, header: str) -> None:
    print(f"\n{header}")
    print(f"values as of {result['asof']}")
    for side in result["sides"].values():
        print(f"\n  {side['team']}")
        for asset in side["assets_in"]:
            print(f"    + {asset['label']:<26} {asset['value']:>7,}  {asset['basis']}")
        for asset in side["assets_out"]:
            print(f"    - {asset['label']:<26} {asset['value']:>7,}  {asset['basis']}")
        print(
            f"    VALUE {side['value_grade']:<3} {side['value_delta']:+8,} "
            f"({side['value_pct']:+.1%})    "
            f"FIT {side['fit_grade']:<3} {side['lineup_delta']:+8,.0f} "
            f"({side['lineup_pct']:+.1%})"
        )
        print(f"    {side['verdict']}")


def cmd_trades(args) -> None:
    conn = db.init_db()
    league_id = _only_league(conn, args.league)
    rows = conn.execute(
        """SELECT txn_id, week, created_ms FROM transactions
            WHERE league_id = ? AND type = 'trade' AND status = 'complete'
            ORDER BY created_ms""",
        (league_id,),
    ).fetchall()
    if not rows:
        print(
            "No completed trades in this league yet.\n"
            "Try a hypothetical instead:\n"
            '  python cli.py propose --give "Player A" --get "Player B"'
        )
        conn.close()
        return
    for row in rows:
        result = grading.grade(conn, league_id, grading.trade_sides(conn, row["txn_id"]))
        _print_grade(result, f"=== week {row['week']} trade {row['txn_id']} ===")
        _print_retro(retro.retrospective(conn, league_id, row["txn_id"]))
    conn.close()


def _print_retro(report: dict) -> None:
    """The ex-post half: what the trade actually did once games were played."""
    weeks = report["weeks_measured"]
    if not weeks:
        print("\n  no games played since this trade yet")
        return
    print(f"\n  --- since the trade (weeks {weeks[0]}-{weeks[-1]}) ---")
    for side in report["sides"].values():
        print(
            f"    {side['team']:<24} {side['total_swing']:+8.1f} pts   "
            f"roster {side['total_roster_swing']:+7.1f}   {side['record_swing']}"
        )
        for flip in side["flips"]:
            print(
                f"      week {flip['week']}: {flip['from']} -> {flip['to']}  "
                f"(scored {flip['actual']} vs {flip['opponent']}; "
                f"without the trade {flip['without_trade']})"
            )
        for player in side["contributions"]["acquired"]:
            print(
                f"      + {player['name']:<22} {player['started_points']:>6.1f} started"
                f"  {player['bench_points']:>6.1f} on the bench"
            )
        print(f"      {side['verdict']}")
    if report["unresolved_picks"]:
        picks = ", ".join(
            f"{p['season']} rd {p['round']}" for p in report["unresolved_picks"]
        )
        print(f"    (picks not yet used, so ungradeable: {picks})")


def cmd_propose(args) -> None:
    conn = db.init_db()
    league_id = _only_league(conn, args.league)
    try:
        sides = grading.build_proposal(
            conn, league_id, give=args.give, get=args.get, from_team=getattr(args, "from_team", None)
        )
    except grading.ProposalError as exc:
        conn.close()
        sys.exit(str(exc))
    # applied=False: the rosters on file do not include this trade, because it
    # has not happened.
    result = grading.grade(conn, league_id, sides, applied=False)
    _print_grade(result, "=== proposed trade ===")
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


def cmd_power(args) -> None:
    """Rank by all-play record: how good a team has been, minus the schedule."""
    conn = db.init_db()
    league_id = _only_league(conn, args.league)
    report = analytics.season_report(conn, league_id)
    if not report:
        print("No games played yet — nothing to rank on results.")
        print("Roster value rankings are on the dashboard in the meantime.")
        conn.close()
        return

    print(
        f"{'#':>2} {'team':<24}{'record':>8}{'all-play':>10}{'exp W':>7}"
        f"{'luck':>7}{'PF':>9}{'lineup':>8}{'swing':>8}"
    )
    for team in report:
        print(
            f"{team['rank']:>2} {team['team']:<24}{team['record']:>8}"
            f"{team['all_play']:>10}{team['expected_wins']:>7.1f}"
            f"{team['luck']:>+7.1f}{team['points_for']:>9.0f}"
            f"{(team['lineup_efficiency'] or 0):>8.1%}{team['rank_gap']:>+8}"
        )
    print(
        "\n  luck    = actual wins minus what the all-play record says they earned"
        "\n  lineup  = share of their best possible score they actually started"
        "\n  swing   = places the real standings flatter them by"
    )
    conn.close()


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

    p_power = sub.add_parser("power", help="luck-adjusted power rankings")
    p_power.add_argument("--league", help="league id or name, if several are synced")
    p_power.set_defaults(func=cmd_power)

    p_trades = sub.add_parser("trades", help="grade every completed trade")
    p_trades.add_argument("--league", help="league id or name, if several are synced")
    p_trades.set_defaults(func=cmd_trades)

    p_prop = sub.add_parser("propose", help="grade a hypothetical trade")
    p_prop.add_argument(
        "--give", action="append", default=[], metavar="PLAYER",
        help="a player you would send away (repeat for several)",
    )
    p_prop.add_argument(
        "--get", action="append", default=[], metavar="PLAYER",
        help="a player you would receive (repeat for several)",
    )
    p_prop.add_argument(
        "--from", dest="from_team", metavar="TEAM",
        help="your team, if the players you are giving up span rosters",
    )
    p_prop.add_argument("--league", help="league id or name, if several are synced")
    p_prop.set_defaults(func=cmd_propose)

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
