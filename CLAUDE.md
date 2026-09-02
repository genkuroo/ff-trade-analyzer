# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A Sleeper fantasy football roster grader and trade analyzer. Two grades per
trade: an **instant** one (market value at the time) and a **retrospective**
one (what the trade actually produced, in points that reached the lineup).
Built primarily for the "Money Hole" dynasty league but multi-league by design.

## Commands

```bash
.venv/bin/python cli.py sync              # pull configured leagues
.venv/bin/python cli.py sync --history    # ...plus every prior season
.venv/bin/python cli.py sync --no-values  # league data only (frequent poll)
.venv/bin/python cli.py values            # market-value snapshot only (daily)
.venv/bin/python cli.py discover <user>   # find league ids for a username
.venv/bin/python cli.py status            # what is in the database
.venv/bin/python app.py                   # dashboard on :5003
```

## Architecture

Flat modules in the style of `../fitness-dashboard`: `db.py` owns the schema,
`ingest.py` owns the sync, `analytics.py` owns read-only computation, `app.py`
is the UI, `sources/` holds one module per external API, and each phase adds a
module rather than a package.

`grading.py` is the phase 2 engine; it depends on `analytics.best_lineup` and
on nothing in `app.py`, so the CLI and the web UI grade identically.

`app.py` never calls an external API. Fetching belongs to `cli.py sync`, so a
page load can never be slow, rate-limited, or broken by someone else's outage.

Two external sources, both free and keyless:

- **Sleeper** (`sources/sleeper.py`) — league, rosters, transactions, weekly
  matchups, drafts. A separate *async* client for the same API exists in
  `../sleeper-discord-bot/sleeperbot/sleeper.py`; it is deliberately not
  shared, because that one lives in a discord.py event loop and this one runs
  in Flask and a CLI. Copying ~80 lines beat coupling two projects.
- **FantasyCalc** (`sources/values.py`) — player and draft-pick values, keyed
  by `sleeperId` so no name matching is needed, and parameterised by league
  shape via `config_key()`.

## Things to know before changing code

- **`player_weeks` is the load-bearing table.** Sleeper returns `starters` (an
  array) and `players_points` (a map) separately; ingest flattens them to one
  row per player-week with a `started` flag. Every retrospective grade depends
  on that flag. Do not "simplify" it away.
- **Value snapshots are keyed by date and league shape, never by league id.**
  Two leagues of the same shape share a board. Grading a trade must use the
  snapshot nearest the trade date, not the latest one — otherwise the grade is
  hindsight, not judgment.
- **All ingest is idempotent** (`INSERT OR REPLACE` on natural keys). Any sync
  must stay safe to re-run; it is meant to be scheduled.
- **Sleeper returns HTTP 200 with a literal `null`** for valid-but-empty
  resources (a week with no transactions). `_get` maps that to `None`; callers
  coerce to `[]`.
- **Dynasty seasons are separate leagues** chained by `previous_league_id`.
  Anything that reasons about history must walk that chain (`sync_history`).
- Taxi and IR players cannot be started, so `roster_slots.slot` distinguishes
  them. A grader that counts taxi players as ordinary depth overrates
  rebuilding teams.
- **`analytics.best_lineup` is shared infrastructure.** Power rankings use it
  now; phase 3's counterfactual replay ("what would this roster have scored
  without the trade?") calls the same function with weekly points instead of
  market values. Keep it taking a generic `score` field.
- **`grade(..., applied=)` is easy to get wrong and fails silently.** It says
  whether the roster snapshot on file already includes the trade. It does for a
  completed trade, it does not for a proposal. Passing the wrong value produces
  a fit delta of exactly zero, which looks like a plausible answer rather than
  a bug.
- **Value and fit are reported separately and must stay that way.** Blending
  them hides the case the project exists to surface: a rebuild trade should win
  on value and lose on fit. Fit is also what handles consolidation, so there is
  no "2-for-1 premium" constant to tune — do not add one.
- Fit is scored against the *size of the deal*, not the size of the roster.
  Against roster value every trade looks like a rounding error.
- Kickers and defenses come back unvalued from FantasyCalc — correct, they have
  no trade value. They are kept in the lineup at zero rather than dropped, so
  their slots don't read as unfillable.

## Conventions

Follows the workspace rules in `../CLAUDE.md` — notably **no AI attribution in
commits or PRs**, and sync the Obsidian project note after a commit/push.
