"""External data sources, and the line between them.

The whole project rests on one division of labour, and it is worth stating
plainly because almost every "where does this number come from?" question
reduces to it:

    Sleeper says what HAPPENED.  FantasyCalc says what it is WORTH.

Sleeper is the system of record for the league. It is authoritative about every
fact: who is on which roster, what was traded, who was started in week 6, what
they scored, where each player was actually drafted. If Sleeper and anything
else disagree about a fact, Sleeper wins by definition -- it *is* the league.

What Sleeper does not have, at all, is any notion of value. There is no trade
calculator, no player worth, no pick value; a survey of its projections
endpoint turns up 92 fields and not one of them is value-shaped. That is the
gap FantasyCalc fills, and the only reason it is here.

FantasyCalc is therefore never consulted about a fact, and Sleeper is never
consulted about worth. Keeping that boundary sharp is what stops the two from
quietly contradicting each other.

----------------------------------------------------------------------------
FROM SLEEPER (facts -- free, keyless, authoritative)

  league / rosters / users   settings, scoring, roster slots, ownership,
                             taxi and IR, records
  transactions               trades, waivers, adds and drops, FAAB, traded picks
  matchups                   per week: the `starters` array AND the
                             `players_points` map -- the pairing that lets a
                             retrospective grade tell banked points from bench
                             points
  drafts                     actual pick numbers, the "what really happened"
                             half of draft analysis
  players/nfl                name, position, team, age, status, search_rank
  projections                ADP (`adp_dd_ppr`, `pos_adp_dd_ppr`) and projected
                             points (`pts_ppr`) -- ADP is used; the projections
                             are available and not yet consumed

FROM FANTASYCALC (worth -- free, keyless, crowd-derived from real trades)

  player value               dynasty, redraft and combined, per league shape
  draft pick value           by projected slot, the other half of a dynasty trade
  overall / position rank    market ordering, i.e. where a player *should* go
  tier, 30-day trend         market context
  trade frequency,           how contested and how agreed-upon a player is
  roster %, value stddev

----------------------------------------------------------------------------
THREE THINGS CALLED "RANK", WHICH ARE NOT THE SAME THING

Conflating these produces confident nonsense, so they are stored separately
and named for their source:

  position_rank   FantasyCalc.  Trade-value order. "WR4" = 4th most valuable WR.
  position_adp    Sleeper.      Draft order. Where WRs actually come off boards.
  (production)    Sleeper /stats. Points actually scored. Not stored yet -- it
                  needs games to have been played.

The gap between the first two is the interesting signal: value rank says who is
better, ADP says who people take, and a player whose ADP badly trails his value
rank is one the market has not caught up to.
"""
