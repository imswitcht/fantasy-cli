"""Sleeper driver.

READS are excellent: Sleeper publishes a real read API, no auth needed, and it
is the best projection source of the three, so we also use it to project the
Yahoo and ESPN teams.

WRITES: Sleeper's public API is explicitly read-only -- "you cannot modify
contents via this API." There is no sanctioned write path at all. This driver
therefore reports can_write_lineup=False unless you have captured the private
request yourself with `ff capture sleeper`, in which case it replays that shape.

Before you bother: Sleeper has a built-in feature called Player AutoSubs that
already does the thing you most want automated. You designate a bench player as
the sub for a starter, and if the starter is inactive Sleeper swaps them for you
at kickoff. `ff sub-plan sleeper` prints the pairings you should set up so the
platform's own feature covers this team.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from ..models import Availability, LineupPlan, Player, Roster, Slot, TeamRef
from .base import Capabilities, NotSupported, Provider, ProviderError

API = "https://api.sleeper.app/v1"
API2 = "https://api.sleeper.com"

SLOT_MAP: dict[str, Slot] = {
    "QB": Slot.QB,
    "RB": Slot.RB,
    "WR": Slot.WR,
    "TE": Slot.TE,
    "K": Slot.K,
    "DEF": Slot.DEF,
    "FLEX": Slot.FLEX,
    "WRRB_FLEX": Slot.WRRB,
    "REC_FLEX": Slot.WRTE,
    "SUPER_FLEX": Slot.SUPERFLEX,
    "BN": Slot.BENCH,
    "IR": Slot.IR,
}

INJURY_MAP = {
    "IR": Availability.INJURED_RESERVE,
    "Out": Availability.OUT,
    "Doubtful": Availability.DOUBTFUL,
    "Questionable": Availability.QUESTIONABLE,
    "PUP": Availability.OUT,
    "Sus": Availability.SUSPENDED,
    "NA": Availability.OUT,
}


class SleeperProvider(Provider):
    name = "sleeper"

    def __init__(self, username: str, season: int, cache_dir: Path,
                 capture_path: Optional[Path] = None):
        self.username = username
        self.season = season
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.capture_path = capture_path
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._players: Optional[dict] = None
        self._user_id: Optional[str] = None
        # One SleeperProvider is shared across every team that needs its
        # projections/crosswalk (ff/tui.py loads all tabs from concurrent
        # background threads), so the lazy caches below need a lock.
        self._lock = threading.Lock()

    @property
    def capabilities(self) -> Capabilities:
        has_capture = bool(self.capture_path and self.capture_path.exists())
        return Capabilities(
            can_read=True,
            can_write_lineup=has_capture,
            write_status="unofficial" if has_capture else "none",
            write_caveat=(
                "Replaying a request captured from the Sleeper web app. Entirely "
                "unsupported and will break when Sleeper ships an update."
                if has_capture else
                "Sleeper's API is read-only by design. Use Sleeper's own AutoSubs "
                "feature instead -- run `ff sub-plan` to see what to set."
            ),
        )

    # ---------------------------------------------------------------- reads

    def _get(self, url: str, **kw) -> Any:
        r = self.session.get(url, timeout=30, **kw)
        r.raise_for_status()
        return r.json()

    def user_id(self) -> str:
        if self._user_id is not None:
            return self._user_id
        with self._lock:
            if self._user_id is None:
                data = self._get(f"{API}/user/{self.username}")
                if not data:
                    raise ProviderError(f"Sleeper user '{self.username}' not found.")
                self._user_id = data["user_id"]
            return self._user_id

    def check_auth(self) -> str:
        uid = self.user_id()
        return f"Sleeper user {self.username} (id {uid}) - public read API, no auth needed"

    def current_week(self) -> int:
        state = self._get(f"{API}/state/nfl")
        return int(state.get("week") or 1)

    def players(self) -> dict:
        """The full NFL player dictionary. ~5MB, so cached on disk for a day."""
        if self._players is not None:
            return self._players
        with self._lock:
            if self._players is not None:
                return self._players
            cache = self.cache_dir / "sleeper_players.json"
            if cache.exists() and time.time() - cache.stat().st_mtime < 86400:
                self._players = json.loads(cache.read_text())
                return self._players
            data = self._get(f"{API}/players/nfl")
            cache.write_text(json.dumps(data))
            self._players = data
            return data

    def projections(self, week: int) -> dict[str, float]:
        """player_id -> projected PPR points. Used for all three platforms."""
        cache = self.cache_dir / f"sleeper_proj_{self.season}_{week}.json"
        with self._lock:
            if cache.exists() and time.time() - cache.stat().st_mtime < 3600:
                return json.loads(cache.read_text())
            out: dict[str, float] = {}
            try:
                rows = self._get(
                    f"{API2}/projections/nfl/{self.season}/{week}",
                    params={"season_type": "regular", "order_by": "ppr"},
                )
                for row in rows or []:
                    pid = str(row.get("player_id"))
                    stats = row.get("stats") or {}
                    pts = stats.get("pts_ppr") or stats.get("pts_half_ppr") or stats.get("pts_std")
                    if pid and pts is not None:
                        out[pid] = float(pts)
                cache.write_text(json.dumps(out))
            except requests.HTTPError as e:
                raise ProviderError(
                    f"Sleeper projections endpoint returned {e.response.status_code}. "
                    "This is an undocumented endpoint and may have moved; "
                    "run with --no-projections to fall back to platform projections."
                ) from e
            return out

    def discover_teams(self) -> list[TeamRef]:
        uid = self.user_id()
        leagues = self._get(f"{API}/user/{uid}/leagues/nfl/{self.season}")
        refs: list[TeamRef] = []
        for lg in leagues or []:
            rosters = self._get(f"{API}/league/{lg['league_id']}/rosters")
            mine = next((r for r in rosters if r.get("owner_id") == uid), None)
            if not mine:
                continue
            refs.append(TeamRef(
                provider="sleeper",
                league_id=lg["league_id"],
                team_id=str(mine["roster_id"]),
                nickname=lg.get("name", "Sleeper league"),
                league_name=lg.get("name"),
                slot_layout=[SLOT_MAP[p] for p in lg.get("roster_positions", [])
                             if SLOT_MAP.get(p, Slot.BENCH).is_starting],
            ))
        return refs

    def get_roster(self, team: TeamRef, week: Optional[int] = None) -> Roster:
        week = week or self.current_week()
        league = self._get(f"{API}/league/{team.league_id}")
        rosters = self._get(f"{API}/league/{team.league_id}/rosters")
        mine = next((r for r in rosters
                     if str(r.get("roster_id")) == str(team.team_id)), None)
        if mine is None:
            raise ProviderError(
                f"Roster {team.team_id} not found in Sleeper league {team.league_id}")

        raw_positions = league.get("roster_positions", [])
        starting_slots = [SLOT_MAP.get(p, Slot.BENCH) for p in raw_positions
                          if SLOT_MAP.get(p, Slot.BENCH).is_starting]
        if not team.slot_layout:
            team.slot_layout = starting_slots
        if not team.league_name:
            team.league_name = league.get("name")

        catalog = self.players()
        proj = self.projections(week)
        kickoffs = _kickoffs(self.session, self.season, week)

        starters: list[str] = mine.get("starters") or []
        all_ids: list[str] = mine.get("players") or []
        reserve: list[str] = mine.get("reserve") or []

        players: list[Player] = []
        for idx, pid in enumerate(starters):
            if not pid or pid == "0":
                continue
            slot = starting_slots[idx] if idx < len(starting_slots) else Slot.FLEX
            p = _build(pid, catalog, proj, kickoffs, slot)
            if p:
                players.append(p)

        for pid in all_ids:
            if pid in starters:
                continue
            slot = Slot.IR if pid in reserve else Slot.BENCH
            p = _build(pid, catalog, proj, kickoffs, slot)
            if p:
                players.append(p)

        return Roster(players=players)

    # --------------------------------------------------------------- writes

    def team_url(self, team: TeamRef) -> str:
        return f"https://sleeper.com/leagues/{team.league_id}/team"

    def apply_lineup(self, plan: LineupPlan, week: Optional[int] = None) -> None:
        if not self.capabilities.can_write_lineup:
            raise NotSupported(
                "Sleeper's API is read-only -- there is no supported way to set a "
                "lineup programmatically.\n\nMake these changes in the Sleeper app:\n"
                + "\n".join(f"  - {m}" for m in plan.moves)
                + "\n\nBetter: run `ff sub-plan` and configure Sleeper's built-in "
                  "AutoSubs so the platform handles inactive starters for you."
            )
        raise NotSupported(
            "Captured-request replay for Sleeper is not implemented. Capturing the "
            "request is the easy half; Sleeper rotates auth on its private endpoints, "
            "so a replay that works today usually fails within days. Recommend "
            "AutoSubs instead."
        )


# ------------------------------------------------------------------ helpers

def _build(pid: str, catalog: dict, proj: dict[str, float],
           kickoffs: dict[str, datetime], slot: Slot) -> Optional[Player]:
    meta = catalog.get(str(pid))
    if not meta:
        return None
    pos = (meta.get("position") or "UNKNOWN").upper()
    if pos == "DST":
        pos = "DEF"
    eligible = {p.upper().replace("DST", "DEF")
                for p in (meta.get("fantasy_positions") or [pos])}
    team_abbr = meta.get("team")
    status = meta.get("injury_status")
    availability = INJURY_MAP.get(status, Availability.ACTIVE if status is None
                                  else Availability.QUESTIONABLE)
    kickoff = kickoffs.get(team_abbr) if team_abbr else None
    if team_abbr and kickoffs and team_abbr not in kickoffs:
        availability = Availability.BYE

    name = meta.get("full_name") or f"{meta.get('first_name','')} {meta.get('last_name','')}".strip()
    return Player(
        platform_id=str(pid),
        name=name or str(pid),
        position=pos,
        nfl_team=team_abbr,
        eligible_positions=eligible or {pos},
        availability=availability,
        injury_note=status,
        kickoff=kickoff,
        projection=proj.get(str(pid)),
        slot=slot,
    )


def _kickoffs(session: requests.Session, season: int, week: int) -> dict[str, datetime]:
    try:
        r = session.get(
            "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
            params={"week": week, "seasontype": 2, "dates": season}, timeout=20)
        r.raise_for_status()
        out: dict[str, datetime] = {}
        for game in r.json().get("events", []):
            start = datetime.fromisoformat(game["date"].replace("Z", "+00:00"))
            for comp in game.get("competitions", [{}])[0].get("competitors", []):
                abbr = comp.get("team", {}).get("abbreviation")
                if abbr:
                    out[abbr] = start
        return out
    except Exception:
        return {}
