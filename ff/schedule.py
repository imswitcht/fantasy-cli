"""Shared NFL schedule/game-state lookup.

Both ff/providers/espn.py and ff/providers/sleeper.py used to independently
hit ESPN's public (unauthenticated) site scoreboard just to get kickoff
times, throwing away the rest of the response. That response already
contains the opponent, a live game-status string, and each side's score for
every team playing that week, so this pulls it all out of one shared call
instead of two near-duplicate ones that only kept the kickoff.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import requests

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"

# ESPN's scoreboard is the canonical abbreviation space every lookup here is
# keyed by. Sleeper's own player catalog disagrees with it for Washington
# ("WAS" vs. ESPN's "WSH", confirmed against live data from both) -- every
# Sleeper player on Washington was landing on the "no schedule entry -> bye"
# fallback below even in week 1. Route every raw provider abbreviation
# through this before using it, so a future mismatch on another team gets
# caught the same way instead of needing its own bug report.
TEAM_ABBR_ALIASES = {"WAS": "WSH"}


def normalize_team_abbr(abbr: Optional[str]) -> Optional[str]:
    if not abbr:
        return abbr
    abbr = abbr.upper()
    return TEAM_ABBR_ALIASES.get(abbr, abbr)


@dataclass
class GameInfo:
    kickoff: Optional[datetime]
    opponent: Optional[str]   # NFL team abbreviation, e.g. "SEA"
    status: str               # "" | "Sun 1:00 PM" | "2:51 - 3rd" | "Final"
    state: str                # "" | "pre" | "in" | "post"
    score: str                # "" pregame | "13-10" (own-opponent) once it's underway


def fetch_nfl_schedule(session: requests.Session, season: int, week: int) -> dict[str, GameInfo]:
    """NFL team abbreviation -> this week's GameInfo. Empty dict on any failure.

    Locks and byes only need this to fail soft (see the try/except this
    replaces in both providers), so callers should treat a missing entry as
    "no data available" rather than an error.
    """
    try:
        r = session.get(SCOREBOARD_URL,
                        params={"week": week, "seasontype": 2, "dates": season},
                        timeout=20)
        r.raise_for_status()
        out: dict[str, GameInfo] = {}
        for game in r.json().get("events", []):
            comp = (game.get("competitions") or [{}])[0]
            kickoff = datetime.fromisoformat(game["date"].replace("Z", "+00:00"))
            status_type = (comp.get("status") or {}).get("type") or {}
            status = status_type.get("shortDetail") or ""
            state = status_type.get("state") or ""

            competitors = comp.get("competitors") or []
            abbrs = [normalize_team_abbr(c.get("team", {}).get("abbreviation"))
                    for c in competitors]
            scores = [c.get("score") for c in competitors]
            for i, abbr in enumerate(abbrs):
                if not abbr:
                    continue
                opponent = next((a for j, a in enumerate(abbrs) if j != i and a), None)
                score = ""
                if state in ("in", "post") and scores[i] is not None:
                    opp_score = next((s for j, s in enumerate(scores) if j != i), None)
                    if opp_score is not None:
                        score = f"{scores[i]}-{opp_score}"
                out[abbr] = GameInfo(kickoff=kickoff, opponent=opponent,
                                     status=status, state=state, score=score)
        return out
    except Exception:
        return {}
