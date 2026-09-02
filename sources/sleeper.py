"""Synchronous client for the public Sleeper API.

Sleeper's read API needs no key and no account -- every endpoint here is a
plain GET. The async version of this client already exists in the
sleeper-discord-bot project; it is deliberately *not* shared, because that one
lives inside a discord.py event loop and this one runs in a Flask request and a
CLI. Copying ~80 lines beats making two projects depend on each other.

Caching matters for one endpoint in particular:

  * ``/players/nfl``  ~5 MB, ~11k entries. Sleeper explicitly asks callers to
    fetch it at most once a day, so it is cached to disk and trimmed.

Docs: https://docs.sleeper.com/
"""

from __future__ import annotations

import json
import logging
import os
import time

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://api.sleeper.app/v1"
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0
PLAYER_CACHE_TTL = 24 * 60 * 60

CACHE_DIR = os.environ.get(
    "FFTA_CACHE", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "cache")
)


class SleeperError(RuntimeError):
    """A Sleeper request failed after exhausting retries."""


def _get(path: str):
    """GET a Sleeper path, retrying transient failures with backoff.

    Sleeper returns 200 with a literal ``null`` body for a valid-but-empty
    resource -- a week with no transactions, most commonly -- so callers get
    ``None`` rather than an exception for that case.
    """
    url = f"{BASE_URL}{path}"
    last_error = None

    for attempt in range(MAX_RETRIES):
        try:
            response = requests.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code == 404:
                return None
            # 429 and 5xx are worth retrying; other 4xx means we asked wrong.
            if response.status_code == 429 or response.status_code >= 500:
                raise SleeperError(f"{response.status_code} from {url}")
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, SleeperError, ValueError) as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                delay = RETRY_BASE_DELAY * (2**attempt)
                log.warning("Sleeper GET %s failed (%s), retrying in %.0fs", path, exc, delay)
                time.sleep(delay)

    raise SleeperError(f"GET {path} failed after {MAX_RETRIES} attempts: {last_error}")


# -- league ----------------------------------------------------------------


def state():
    """Current NFL season state -- most importantly, the active week."""
    return _get("/state/nfl") or {}


def league(league_id: str) -> dict:
    data = _get(f"/league/{league_id}")
    if data is None:
        raise SleeperError(f"League {league_id} not found. Check the league id.")
    return data


def users(league_id: str) -> list:
    return _get(f"/league/{league_id}/users") or []


def rosters(league_id: str) -> list:
    return _get(f"/league/{league_id}/rosters") or []


def transactions(league_id: str, week: int) -> list:
    return _get(f"/league/{league_id}/transactions/{week}") or []


def matchups(league_id: str, week: int) -> list:
    return _get(f"/league/{league_id}/matchups/{week}") or []


def traded_picks(league_id: str) -> list:
    """Every pick that has changed hands, including in past seasons."""
    return _get(f"/league/{league_id}/traded_picks") or []


def drafts(league_id: str) -> list:
    return _get(f"/league/{league_id}/drafts") or []


def draft_picks(draft_id: str) -> list:
    return _get(f"/draft/{draft_id}/picks") or []


def adp(season: str, week: int = 1) -> dict:
    """Average draft position per player, from Sleeper's projections endpoint.

    Undocumented but public, and the only free source of real ADP found -- the
    market-value API returns its ``adp`` field empty, and rank is not the same
    thing as ADP (rank says who is better, ADP says where people actually take
    them, and the gap between those two is the interesting part).

    ``adp_dd_ppr`` is dynasty-draft PPR, which happens to match Money Hole's
    format exactly. Sleeper exposes no other variant here, so a redraft league
    would be reading a dynasty ADP -- that is recorded in the field name rather
    than papered over.

    Players Sleeper considers undrafted carry a sentinel of 1000; those are
    dropped rather than stored, since "undrafted" is not a draft position.
    """
    raw = _get(f"/projections/nfl/regular/{season}/{week}") or {}
    out = {}
    for player_id, row in raw.items():
        overall = row.get("adp_dd_ppr")
        if overall is None or overall >= 999:
            continue
        out[player_id] = {
            "adp": overall,
            "position_adp": row.get("pos_adp_dd_ppr"),
        }
    return out


def user_leagues(username: str, season: str) -> list:
    """Every NFL league a username is in for a season -- used to discover ids."""
    user = _get(f"/user/{username}")
    if not user:
        raise SleeperError(f"No Sleeper user named {username!r}")
    return _get(f"/user/{user['user_id']}/leagues/nfl/{season}") or []


# -- players ---------------------------------------------------------------


def players() -> dict:
    """Player id -> trimmed record, cached on disk for a day.

    The upstream payload is ~5 MB of mostly-unused fields (college, height,
    rotowire ids...). Trimming before it hits disk keeps the cache near 1 MB.
    """
    cache_path = os.path.join(CACHE_DIR, "players.json")
    cached = _load_cache(cache_path)
    if cached is not None:
        return cached

    log.info("Fetching the Sleeper player catalog (~5 MB, once per day)")
    raw = _get("/players/nfl") or {}
    trimmed = {pid: _trim_player(pid, data) for pid, data in raw.items()}
    _save_cache(cache_path, trimmed)
    return trimmed


def _trim_player(player_id: str, data: dict) -> dict:
    name = data.get("full_name")
    if not name:
        # Team defenses have no full_name; they arrive as first="San Francisco",
        # last="49ers" under a player_id that is the team abbreviation.
        parts = [data.get("first_name") or "", data.get("last_name") or ""]
        name = " ".join(part for part in parts if part).strip()
    return {
        "name": name or player_id,
        "position": data.get("position") or "",
        "team": data.get("team") or "FA",
        "age": data.get("age"),
        "status": data.get("status") or "",
    }


def _load_cache(path: str):
    try:
        if time.time() - os.path.getmtime(path) > PLAYER_CACHE_TTL:
            return None
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        # A missing or half-written cache is not an error -- just refetch.
        return None


def _save_cache(path: str, payload) -> None:
    # Write-then-rename so a crash mid-write can't leave a truncated cache that
    # the next run would happily load.
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Could not write cache %s: %s", path, exc)
