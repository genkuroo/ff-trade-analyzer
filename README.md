# ff-trade-analyzer

Grades fantasy football rosters and trades for Sleeper leagues — and, unlike
most trade graders, goes back afterwards and scores how each trade *actually*
worked out.

Built for a 10-team 1QB PPR dynasty league ("Money Hole"), but nothing is
hardcoded to it: point it at any Sleeper league id and it adapts.

## Why it exists

Every fantasy platform will tell you a trade was "unbalanced" the moment it
happens. None of them come back in week 12 and tell you who actually won it.
This does both:

- **Instant grade** — what the market says each side gave up, adjusted for
  roster fit and the premium on consolidating talent into one lineup slot.
- **Retrospective grade** — what each side actually got: points that reached
  the starting lineup, measured against what the roster would have scored had
  the trade never happened.

## How it works

Sleeper's read API is public, keyless, and generous. It gives up the league,
its rosters, every transaction, and — the important part — every week's
`starters` array alongside a `players_points` map. That last pairing is what
makes the retrospective grade honest: a receiver who drops 25 on your bench
earned you nothing, and this can tell the difference.

The one thing Sleeper does *not* provide is any notion of player **value** —
no projections, no rankings, nothing forward-looking. That comes from
[FantasyCalc](https://fantasycalc.com), a free keyless API whose values are
derived from real trades across thousands of leagues. Two things make it the
right choice: every row carries a `sleeperId`, so players join directly with no
name matching; and values are parameterised by league shape (dynasty/redraft,
1QB/superflex, team count, PPR), so each league is graded against a board built
for a league like it. It prices draft picks too, which is half of every dynasty
trade.

Values are **snapshotted daily** rather than overwritten. Grading a trade
against the values that were true on the day it was made is the difference
between judging a decision and judging with hindsight.

## Status

| Phase | What | State |
|---|---|---|
| 1 | Sleeper + FantasyCalc ingest into SQLite | done |
| 2 | Instant trade grade (value, roster fit, consolidation) | next |
| 3 | Retrospective grade (counterfactual lineup replay) | |
| 4 | Power rankings with luck adjustment | |
| 5 | Flask dashboard | partial — rankings + moves |
| 6 | Discord slash commands in `sleeper-discord-bot` | |

Phase 5 landed early in skeleton form because the app is self-hosted: there has
to be something for the Pi to serve. It currently shows power rankings and
recent moves; the trade pages arrive with phases 2 and 3.

### Power rankings

Teams are ranked by **lineup value** — the best legal starting lineup they could
field today — rather than the sum of everyone they own. The two disagree more
than you would expect, and the gap is informative: a team carrying a lot of
value it cannot start is exactly the team that should be consolidating. The
lineup solver fills the most restrictive slots first (a dedicated RB slot before
a FLEX), which at real lineup sizes gives the true optimum without a
combinatorial search. It is written to be reused: phase 3 replays a week's
scoring with and without a trade using the same function.

## Setup

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.json config.json     # then add your league id
.venv/bin/python cli.py discover <your-sleeper-username>   # finds league ids
.venv/bin/python cli.py sync
.venv/bin/python cli.py status
```

`sync --history` walks a dynasty league backwards through `previous_league_id`
and pulls every prior season too — Sleeper models each season as its own
league, so that chain is the only route to old trade history.

## Layout

```
db.py           schema + connection; player_weeks is the table that matters
ingest.py       idempotent sync of a league end to end
analytics.py    read-only computations, incl. the lineup solver
app.py          Flask UI (never calls an API — reads the database only)
cli.py          sync / values / discover / status
config.py       league list, from FFTA_LEAGUES or config.json
sources/
  sleeper.py    keyless Sleeper client, disk-caches the 5 MB player catalog
  values.py     FantasyCalc values + draft-pick label parsing
```

## Deployment

Runs on a Raspberry Pi 400 as part of the [homelab-pi](https://github.com/genkuroo/homelab-pi)
stack: one container serving the dashboard, with systemd timers running
`cli.py` inside that same container on two different schedules.

Splitting the schedules is the point. League data is polled every 15 minutes
in-season so a trade shows up quickly; market values are snapshotted once a day,
because that is how fast they actually move and because re-snapshotting on every
poll would just overwrite the day's record with a near-identical one.

```
FFTA_LEAGUES=<id>:<label>,<id>:<label>   # leagues to track
FFTA_DB=/data/ffta.db                    # SQLite file (a Docker volume)
FFTA_CACHE=/data/cache                   # the 5 MB player catalog
```

## Known limits

- FantasyCalc carries ~400 fantasy-relevant players, so kickers, defenses, and
  deep bench flyers come back unvalued. That is the correct answer for trade
  purposes (a kicker has no trade value) but means "roster value" is starters
  and real depth, not a headcount.
- Sleeper tells you a traded pick's season and round but never its slot, while
  FantasyCalc prices picks by projected slot (`2027 1st (Early)`). Reconciling
  the two requires projecting where the owning team will finish — handled in
  the valuation layer, not the ingest.
