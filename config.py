"""Where the list of leagues to track comes from.

Two sources, in priority order, because the two places this runs want
different things:

  * ``FFTA_LEAGUES`` environment variable -- how it runs on the Pi, where the
    homelab stack keeps all configuration in a gitignored ``.env`` and no
    app is expected to carry a config file of its own.
  * ``config.json`` -- how it runs on a laptop, where editing a JSON file beats
    exporting a variable every time you open a shell.

Format of the env var is ``id:label`` pairs, comma separated. The label is
optional and only affects log output::

    FFTA_LEAGUES=1234567890123456789:moneyhole,9876543210987654321:dynasty2
"""

from __future__ import annotations

import json
import os

CONFIG_PATH = os.environ.get(
    "FFTA_CONFIG", os.path.join(os.path.dirname(__file__), "config.json")
)


class ConfigError(RuntimeError):
    """No leagues are configured anywhere."""


def leagues() -> list[dict]:
    """Return [{id, label}, ...] for every configured league."""
    from_env = _from_env()
    if from_env:
        return from_env

    try:
        with open(CONFIG_PATH, encoding="utf-8") as handle:
            entries = (json.load(handle) or {}).get("leagues") or []
    except FileNotFoundError:
        raise ConfigError(
            f"No leagues configured. Either set FFTA_LEAGUES, or copy "
            f"config.example.json to {CONFIG_PATH} and add your league id.\n"
            f"`python cli.py discover <sleeper-username>` will find it."
        ) from None

    parsed = [
        {"id": str(e["id"]), "label": e.get("label") or str(e["id"])}
        for e in entries
        if e.get("id") and not str(e["id"]).startswith("YOUR_")
    ]
    if not parsed:
        raise ConfigError(f"{CONFIG_PATH} lists no usable league ids.")
    return parsed


def _from_env() -> list[dict]:
    raw = (os.environ.get("FFTA_LEAGUES") or "").strip()
    if not raw:
        return []
    parsed = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        league_id, _, label = chunk.partition(":")
        league_id = league_id.strip()
        if league_id:
            parsed.append({"id": league_id, "label": label.strip() or league_id})
    return parsed
