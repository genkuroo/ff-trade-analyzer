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

## Where each number comes from

One division of labour underpins everything:

> **Sleeper says what happened. FantasyCalc says what it is worth.**

Sleeper is the system of record for the league — rosters, trades, lineups,
scores, actual draft picks, and (via an undocumented projections endpoint) real
ADP. If Sleeper disagrees with anything else about a *fact*, Sleeper wins; it
*is* the league.

What Sleeper has no notion of is **value**. There is no trade calculator and no
player worth anywhere in its API — 92 fields on the projections endpoint and not
one of them is value-shaped. That gap is the only reason a second source exists.

| | Sleeper | FantasyCalc |
|---|---|---|
| League settings, rosters, ownership | ✅ | |
| Trades, waivers, FAAB, traded picks | ✅ | |
| Weekly `starters` + `players_points` | ✅ | |
| Actual draft picks | ✅ | |
| **ADP** (`adp_dd_ppr`) | ✅ | ✗ (field exists, always null) |
| Projected points | ✅ (unused so far) | |
| **Player trade value** | ✗ none | ✅ |
| **Draft pick value** | ✗ none | ✅ |
| Value rank, tier, 30-day trend | | ✅ |
| Trade frequency, roster %, volatility | | ✅ |

FantasyCalc is never asked about a fact; Sleeper is never asked about worth.

### Three different things called "rank"

Conflating these produces confident nonsense, so they are stored separately:

- **`position_rank`** (FantasyCalc) — trade-value order. "WR4" = 4th most
  valuable WR to own.
- **`position_adp`** (Sleeper) — draft order. Where WRs actually come off boards.
- **production rank** (Sleeper `/stats`) — points actually scored. Not stored
  yet; it needs games to have been played.

The gap between the first two is the signal worth watching: value rank says who
is *better*, ADP says who people *take*, and a player whose ADP badly trails his
value rank is one the market hasn't caught up to.

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
| 2 | Instant trade grade (value, roster fit, consolidation) | done |
| 3 | Retrospective grade (counterfactual lineup replay) | next |
| 4 | Power rankings with luck adjustment | |
| 5 | Flask dashboard | partial — rankings, trades, machine |
| 6 | Discord slash commands in `sleeper-discord-bot` | |

Phase 5 landed early in skeleton form because the app is self-hosted: there has
to be something for the Pi to serve. It currently shows power rankings and
recent moves; the trade pages arrive with phases 2 and 3.

### The two grades

Every trade gets **two** grades, kept separate rather than blended into one
composite letter:

- **Value** — market value received minus market value given away.
- **Fit** — how much of that value actually reached the starting lineup.

They are separate because in a dynasty league they legitimately disagree, and
the disagreement is the whole story. A rebuilding team that trades a 29-year-old
star for two future firsts *should* win on value and lose on fit; that is a good
trade, not a C. Collapsing both into one number would call it a wash and say
nothing.

Fit also does the work that trade graders usually fake with an invented
"consolidation premium". Two bench players contribute nothing to a starting
lineup, so a 2-for-1 that turns depth into a starter shows up as a fit gain on
its own — no magic multiplier required.

```
The Waiver Wire
  + Carnell Tate      3,817
  - DJ Moore          2,466
  - Travis Etienne    3,227
  VALUE F  -1,876 (-33.0%)    FIT A-  +590 (+10.4%)
  Paid above market to improve the lineup now — defensible for a
  contender, expensive for anyone else.
```

### Valuing draft picks

This is the awkward join in the project. Sleeper records a traded pick as season
+ round + whose pick it originally was, and never its slot, because the slot does
not exist yet. FantasyCalc prices picks *by* slot — a 2027 early first is worth
nearly double a late one. So the slot has to be projected from how good the
original owner is, worst team picking first, with three levels of confidence:
an exact slot when the draft order is known, an early/mid/late tier projected
from standings, or the round's blended value when the market prices no tiers
that far out. The grader reports which one it used rather than presenting a
projection as a fact.

FAAB is priced off the league's own lineups: a full budget is worth roughly the
weakest player anyone is actually starting, because that is what the money is
for.

### The players page

`/league/<id>/players` puts the raw observations and the derived movement side
by side, split by a visible divider: everything left of it is stored as-is,
everything right of it is computed at read time. Sortable by any column,
filterable by position, team, and window (1–30 days).

```
Matthew Stafford QB LA   value 1,510  ovr 147  QB24  ADP 117  pick 230
                         Δ value −46 (−3.0%)   Δ rank −7   Δ ADP +113
```

That last number is the interesting one: he lasted 113 picks past his ADP, which
in a 1QB dynasty startup is exactly what should happen to a 38-year-old
quarterback. Alongside it, a draft report ranks the biggest reaches and the
biggest fallers.

### The trade machine

`/league/<id>/machine` grades a hypothetical before you offer it. Everything
lives in the query string and nothing is written, so a graded trade is just a
link you can paste into the league chat.

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

.venv/bin/python cli.py trades                          # grade real trades
.venv/bin/python cli.py propose --give "Puka Nacua" --get "Ja'Marr Chase"
```

### Seeing it work without any trades

A first-year league has no trade history, and its picks and FAAB have never
moved — so three of the grader's paths have nothing real to run against. The
seeder builds a synthetic league with real players and real market values, and
four trades chosen to exercise every shape: star-for-star, 2-for-1
consolidation, a rebuild for future picks, and a FAAB sweetener.

```bash
FFTA_DB=demo.db .venv/bin/python scripts/seed_demo.py
FFTA_DB=demo.db .venv/bin/python cli.py trades
FFTA_DB=demo.db .venv/bin/python app.py     # dashboard on :5003
```

`sync --history` walks a dynasty league backwards through `previous_league_id`
and pulls every prior season too — Sleeper models each season as its own
league, so that chain is the only route to old trade history.

## Layout

```
db.py           schema + connection; player_weeks is the table that matters
ingest.py       idempotent sync of a league end to end
analytics.py    read-only computations, incl. the lineup solver
grading.py      instant trade grading: value, fit, picks, FAAB
app.py          Flask UI (never calls an API — reads the database only)
cli.py          sync / values / trades / propose / discover / status
config.py       league list, from FFTA_LEAGUES or config.json
sources/
  sleeper.py    keyless Sleeper client, disk-caches the 5 MB player catalog
  values.py     FantasyCalc values + draft-pick label parsing
scripts/
  seed_demo.py  synthetic league + trades, for demoing the grader
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
