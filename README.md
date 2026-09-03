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
| 3 | Retrospective grade (counterfactual lineup replay) | done |
| 4 | Power rankings with luck adjustment | done |
| 5 | Flask dashboard | partial — rankings, trades, machine, players |
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

### The retrospective grade

This is the part nobody else does. Every platform grades a trade the moment it
happens and then never mentions it again.

The naive version — adding up the points the traded players scored afterwards —
is close to meaningless. A receiver who drops 25 on your bench earned you
nothing, and if you traded away a running back you were never going to start,
losing his production cost you zero.

So the season gets **replayed instead**. For every week after the trade, the
roster is rebuilt as it would have been, every lineup decision the manager
actually made is kept, and only the slots the trade vacated are refilled from
the pre-trade roster. That yields a score directly comparable to what really
happened — which means a won or lost game can honestly be recomputed:

```
Prevent Defense                 VALUE C-  −530     ← graded at the time
  + Tetairoa McMillan
  − Chase Brown

  since the trade (weeks 3–10)  +23.1 points, +2 wins
    week 3: loss → win   scored 90.77 vs 89.95, without the trade 87.77
    week 9: loss → win   scored 106.79 vs 103.88, without the trade 100.46
```

A **C-** at the time; two extra wins in reality. That gap is the whole point of
the project.

Two swings are reported, because conflating them produces nonsense:

- **swing** — measured on what actually happened. The only basis on which a
  head-to-head result can be recomputed.
- **roster ceiling** — best-possible versus best-possible. How much better the
  roster got, whether or not the manager used it.

They can disagree sharply, and the disagreement is informative: acquiring
someone who raises your ceiling but never leaves your bench is a real upgrade
worth zero actual points.

**Roster-size correction.** The replay is built from the roster as it stands
now, which already contains every waiver pickup made since the trade — including
ones made to cover the hole the trade created. Left alone it would hand the
traded-away player back *and* keep the replacement signed to replace him, when
only one of the two ever existed. Rosters are capped, so whenever the
counterfactual comes out oversized, the most recent post-trade pickups are
dropped until the size matches.

How much this matters, measured rather than assumed: across the demo season it
fires a dozen times and moves the answer by **0.0 points**. Only the slots a
trade vacated get refilled, and that slot goes to the returned player in 16 of
28 cases and an existing bench player in the rest — almost never to the waiver
claim being displaced. A design choice made for a different reason turns out to
make the model largely immune to the bias. The correction stays because it is
right and free, not because it rescues anything.

### Valuing draft picks

This is the awkward join in the project. Sleeper records a traded pick as season
+ round + whose pick it originally was, and never its slot, because the slot does
not exist yet. FantasyCalc prices picks *by* slot — a 2027 early first is worth
nearly double a late one. So the slot has to be projected from how good the
original owner is, worst team picking first, with three levels of confidence:
an exact slot when the draft order is known, an early/mid/late tier projected
from the owner, or the round's blended value when the market prices no tiers
that far out. The grader reports which one it used rather than presenting a
projection as a fact.

**Projecting the tier uses record and roster value together, weighted toward
record** (70/30 once there is a season to read). Record is what literally sets
the draft order; roster value is the corrective, because a team at 2-6 with the
best roster in the league is far likelier to climb than one that is 2-6 on
merit, and their pick should not be priced as a premium selection.

The weighting is not fixed. In week 1 a record carries no information — everyone
is 0-0 — so the projection leans entirely on roster value and slides toward
record as the sample grows, reaching full weight around six games. Preseason
behaviour is then a special case of the same formula rather than a separate
branch:

```
games   record weight   0-8 team (good roster) / 4-4 team (worst roster)
    0             0%    late  / early      ← roster value only
    2            23%    mid   / early
    6            70%    early / mid        ← record has taken over
```

**Picks are tradeable in the trade machine and from the CLI**, alongside FAAB.
Ownership is derived from Sleeper's `traded_picks` — which lists only picks that
have *moved* — layered on the assumption that every other pick is still held by
the team whose pick it is.

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

### Two rosters, kept apart

A team has two rosters and conflating them causes bugs:

- **active** — who is on it now. Power rankings and the trade machine use this.
- **all-time** — everyone it has ever held, with the weeks they were held.
  Trade grading needs this one, because a player's points only count for you
  while he was actually yours; credit stops the week he was dropped or flipped
  on.

`/league/<id>/team/<roster_id>` shows both, with how each player arrived (draft,
trade, waiver, free agency) and how long they stayed. Stints are runs of
consecutive weeks rather than a first-and-last-seen range, because players do
leave and come back and `MIN`/`MAX` would silently merge two separate spells
into one that never happened.

The source is `player_weeks` — Sleeper's own weekly record of who rostered whom
— so this is authoritative rather than reconstructed, and it reaches back
further than this project's own daily snapshots.

### The trade machine

`/league/<id>/machine` grades a hypothetical before you offer it. Everything
lives in the query string and nothing is written, so a graded trade is just a
link you can paste into the league chat.

### Luck-adjusted standings

A fantasy record is part team, part draw. You can score the second-most points in
the league most weeks and sit at 3-5 because you kept running into whoever went
off. **All-play** asks a different question: each week, how many of the other
teams would you have beaten? Over a season that deletes the schedule entirely,
because everyone faces the same opponent set — all of them, every week.

The gap between the wins a team has and the wins their scoring earned is **luck**,
in the literal sense of results that had nothing to do with how they played.

From the demo season, two teams with the *identical* all-play record:

```
Injury Report        6-4    all-play 39-51    luck +1.7
Play Action Heroes   2-8    all-play 39-51    luck −2.3
```

Same scoring quality. Four games apart in the standings, entirely schedule.

Reported alongside, deliberately unblended: **lineup efficiency** (points started
over the most the roster could have scored — the one number a manager fully
controls), **consistency** (week-to-week standard deviation), and the
market-value ranking, which is forward-looking where the rest is backward-looking.

The luck meter is a diverging bar on a warm/cool pair rather than red/green —
luck is polarity, not virtue, and being unlucky is not a failure. The pair is
validated for colour-blindness (CVD ΔE 27.9 protan, well above the 8 threshold)
and the number is always printed next to the bar, so nothing rests on colour
alone.

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
retro.py        retrospective grading: counterfactual replay, flipped games
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
