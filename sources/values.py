"""Player and draft-pick market values from FantasyCalc.

Sleeper gives you no valuation of any kind -- no projections, no rankings, no
trade values. That gap is the whole reason this module exists.

FantasyCalc is a free, keyless public API whose values are derived from real
trades made across thousands of leagues, so a grade based on them reflects what
the market actually pays rather than one analyst's board. Two properties make
it the right fit here:

  * every row carries a ``sleeperId``, so it joins to Sleeper data directly and
    we never have to match players by name (the usual source of pain);
  * the values are parameterised by league shape -- dynasty vs redraft, 1QB vs
    superflex, team count, PPR -- so each league is graded against values built
    for a league like it.

Docs (informal): https://fantasycalc.com
"""

from __future__ import annotations

import logging
import re

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.fantasycalc.com/values/current"
REQUEST_TIMEOUT = 30

ROUND_WORDS = {"1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5}

# "2026 Pick 1.01"  -> exact slot, once the rookie draft order is known
_EXACT = re.compile(r"^(\d{4})\s+Pick\s+(\d+)\.(\d+)$")
# "2027 1st (Early)" or "2027 1st" -> a pick whose slot is still a projection
_GENERIC = re.compile(r"^(\d{4})\s+(\d(?:st|nd|rd|th))(?:\s+\((\w+)\))?$")


def config_key(is_dynasty: bool, num_qbs: int, num_teams: int, ppr: float) -> str:
    """A stable name for one league shape, used to key stored values.

    Two leagues with the same shape share a value set, so a ten-team 1QB PPR
    dynasty league costs one fetch no matter how many of them you track.
    """
    kind = "dyn" if is_dynasty else "red"
    return f"{kind}_{num_qbs}qb_{num_teams}tm_ppr{ppr:g}"


def fetch(is_dynasty: bool, num_qbs: int, num_teams: int, ppr: float) -> list:
    """Fetch the current value board for one league shape."""
    params = {
        "isDynasty": str(bool(is_dynasty)).lower(),
        "numQbs": num_qbs,
        "numTeams": num_teams,
        "ppr": ppr,
        "includePicks": "true",
    }
    response = requests.get(BASE_URL, params=params, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return response.json()


def split(rows: list) -> tuple[list, list]:
    """Separate the value board into (players, picks).

    FantasyCalc returns draft picks inline with players under the pseudo
    position ``PICK``. They need different handling -- a player joins on
    sleeperId, a pick has to be reconciled against Sleeper's season+round -- so
    they are split here rather than everywhere downstream.
    """
    players, picks = [], []
    for row in rows:
        player = row.get("player") or {}
        if player.get("position") == "PICK":
            season, rnd, slot = parse_pick_label(player.get("name") or "")
            picks.append(
                {
                    "label": player.get("name"),
                    "season": season,
                    "round": rnd,
                    "slot": slot,
                    "value": row.get("value"),
                }
            )
        elif player.get("sleeperId"):
            players.append(
                {
                    "player_id": str(player["sleeperId"]),
                    "name": player.get("name"),
                    "position": player.get("position"),
                    "value": row.get("value"),
                    "redraft_value": row.get("redraftValue"),
                    "overall_rank": row.get("overallRank"),
                    "position_rank": row.get("positionRank"),
                    "trend_30day": row.get("trend30Day"),
                    "tier": row.get("maybeTier"),
                }
            )
    return players, picks


def parse_pick_label(label: str) -> tuple[str | None, int | None, str | None]:
    """Turn a FantasyCalc pick label into (season, round, slot).

    Three shapes exist, and the slot is the interesting part:

        "2026 Pick 1.01"   -> ("2026", 1, "01")     exact -- draft order known
        "2027 1st (Early)" -> ("2027", 1, "early")  projected tier
        "2027 1st"         -> ("2027", 1, None)     unknown slot, blended value

    Sleeper only ever tells us a traded pick's season and round, never its
    slot, so the ``None`` case is the honest default and the tiers are what we
    upgrade to once we can guess the owner's finish. Returns all-None for a
    label we don't recognise rather than raising, so a new format upstream
    degrades to "no value for that pick" instead of breaking a sync.
    """
    exact = _EXACT.match(label.strip())
    if exact:
        return exact.group(1), int(exact.group(2)), exact.group(3)

    generic = _GENERIC.match(label.strip())
    if generic:
        rnd = ROUND_WORDS.get(generic.group(2))
        slot = (generic.group(3) or "").lower() or None
        return generic.group(1), rnd, slot

    log.warning("Unrecognised FantasyCalc pick label: %r", label)
    return None, None, None
