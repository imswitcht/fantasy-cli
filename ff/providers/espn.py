"""ESPN Fantasy Football driver.

READS are solid: the v3 endpoints under lm-api-reads.fantasy.espn.com are
stable and widely used, authenticated with your own espn_s2 + SWID cookies.

WRITES are the honest weak point. ESPN publishes no API and no documentation.
The payload below matches the request the ESPN web app itself sends when you
drag a player between slots, but it is reverse-engineered and ESPN can change
it without notice. Two consequences, both handled here:

  1. Nothing is written unless you pass --apply, and every write is verified by
     re-reading the roster afterward. A silently-rejected write fails loudly.
  2. If ESPN changes the shape, run `ff capture espn` -- it walks you through
     copying the real request out of your browser's devtools, and this driver
     will use the captured shape instead of the built-in guess.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from ..models import Availability, LineupPlan, Player, Roster, Slot, TeamRef
from ..schedule import GameInfo, fetch_nfl_schedule
from .base import AuthError, Capabilities, Provider, ProviderError

READ_HOST = "https://lm-api-reads.fantasy.espn.com"
WRITE_HOST = "https://lm-api-writes.fantasy.espn.com"

# ESPN lineupSlotId -> canonical slot. Long-standing and stable.
SLOT_BY_ID: dict[int, Slot] = {
    0: Slot.QB,
    2: Slot.RB,
    3: Slot.WRRB,
    4: Slot.WR,
    5: Slot.WRTE,
    6: Slot.TE,
    7: Slot.SUPERFLEX,   # ESPN calls this OP (offensive player)
    16: Slot.DEF,
    17: Slot.K,
    20: Slot.BENCH,
    21: Slot.IR,
    23: Slot.FLEX,
}
ID_BY_SLOT: dict[Slot, int] = {}
for _id, _slot in SLOT_BY_ID.items():
    ID_BY_SLOT.setdefault(_slot, _id)

POSITION_BY_ID = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "DEF"}

# Slots that map to exactly one position. Only these may add a position to a
# player's eligibility -- multi-position slots (FLEX, WR/TE, RB/WR, OP) say
# where a player may be *placed*, not what he *is*.
SINGLE_POSITION_SLOTS = {Slot.QB, Slot.RB, Slot.WR, Slot.TE, Slot.K, Slot.DEF}

PRO_TEAM_ABBR = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA",
    16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI",
    23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}

INJURY_MAP = {
    "ACTIVE": Availability.ACTIVE,
    "NORMAL": Availability.ACTIVE,
    "QUESTIONABLE": Availability.QUESTIONABLE,
    "DOUBTFUL": Availability.DOUBTFUL,
    "OUT": Availability.OUT,
    "INJURY_RESERVE": Availability.INJURED_RESERVE,
    "SUSPENSION": Availability.SUSPENDED,
    "BEREAVEMENT": Availability.OUT,
    "DAY_TO_DAY": Availability.QUESTIONABLE,
}


class EspnProvider(Provider):
    name = "espn"

    def __init__(self, espn_s2: str, swid: str, season: int,
                 capture_path: Optional[Path] = None):
        if not espn_s2 or not swid:
            raise AuthError(
                "ESPN needs espn_s2 and SWID cookies. Run `ff auth espn` for "
                "step-by-step instructions on pulling them from your browser."
            )
        self.season = season
        # SWID is expected with braces; add them if the user stripped them.
        self.swid = swid if swid.startswith("{") else "{" + swid.strip("{}") + "}"
        self.session = requests.Session()
        self.session.cookies.update({"espn_s2": espn_s2, "SWID": self.swid})
        self.session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "X-Fantasy-Source": "kona",
            "X-Fantasy-Platform": "kona-PROD",
        })
        self.capture_path = capture_path

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_read=True,
            can_write_lineup=True,
            write_status="unofficial",
            write_caveat=(
                "ESPN has no public API. This uses your session cookies against "
                "the same endpoint the ESPN website uses. It can break at any time "
                "and your cookies expire roughly monthly."
            ),
        )

    # ---------------------------------------------------------------- reads

    def _league_url(self, league_id: str) -> str:
        return (f"{READ_HOST}/apis/v3/games/ffl/seasons/{self.season}"
                f"/segments/0/leagues/{league_id}")

    def _get(self, league_id: str, views: list[str],
             params: Optional[dict] = None) -> dict:
        p: dict[str, Any] = {"view": views}
        if params:
            p.update(params)
        r = self.session.get(self._league_url(league_id), params=p, timeout=30)
        if r.status_code == 401:
            raise AuthError(
                "ESPN rejected your cookies (401). They have most likely expired -- "
                "re-run `ff auth espn` and paste fresh values."
            )
        if r.status_code == 404:
            raise ProviderError(
                f"ESPN league {league_id} not found for season {self.season}. "
                "Check the league id and that the season year is right."
            )
        r.raise_for_status()
        return r.json()

    def check_auth(self) -> str:
        r = self.session.get(
            f"{READ_HOST}/apis/v3/games/ffl/seasons/{self.season}/segments/0/leagues",
            params={"view": "mTeam"}, timeout=30)
        if r.status_code in (401, 403):
            raise AuthError("ESPN cookies rejected. Re-run `ff auth espn`.")
        return f"ESPN cookies accepted (SWID {self.swid[:10]}...)"

    def current_week(self) -> int:
        # ESPN reports the active scoring period on any league payload; the
        # caller usually has a league handy, so this is a fallback only.
        return _nfl_week_estimate(self.season)

    def discover_teams(self) -> list[TeamRef]:
        raise ProviderError(
            "ESPN cannot list your leagues from the API. Open your ESPN fantasy "
            "team in a browser -- the URL contains leagueId and teamId. Put those "
            "in config.toml."
        )

    def get_roster(self, team: TeamRef, week: Optional[int] = None) -> Roster:
        week = week or self.current_week()
        data = self._get(
            team.league_id,
            ["mRoster", "mTeam", "mSettings", "mMatchupScore"],
            {"scoringPeriodId": week},
        )

        entry = next((t for t in data.get("teams", [])
                      if str(t.get("id")) == str(team.team_id)), None)
        if entry is None:
            available = [str(t.get("id")) for t in data.get("teams", [])]
            raise ProviderError(
                f"Team id {team.team_id} not in ESPN league {team.league_id}. "
                f"Team ids present: {', '.join(available)}"
            )

        if not team.slot_layout:
            team.slot_layout = _slot_layout_from_settings(data.get("settings", {}))
        if not team.league_name:
            team.league_name = data.get("settings", {}).get("name")

        schedule = fetch_nfl_schedule(self.session, self.season, week)
        players: list[Player] = []
        for e in entry.get("roster", {}).get("entries", []):
            p = _parse_player(e, week, schedule)
            if p:
                players.append(p)
        return Roster(players=players)

    def player_info(self, platform_id: str) -> dict:
        """Best-effort ESPN clips + player-card link for the TUI info panel.

        Uses ESPN's public per-athlete overview endpoint. Its numeric id is
        the same as our own platform_id for ESPN players (verified against
        real data: Joe Burrow is 3915511 in both the fantasy roster payload
        and this endpoint). There is no working per-player *news* filter on
        ESPN's public API -- the news endpoint's `athlete=` query param is
        silently ignored and just returns generic top NFL news regardless of
        id, so this uses the athlete page's own "videos" list instead, which
        genuinely is player-specific.
        """
        r = self.session.get(
            "https://site.web.api.espn.com/apis/common/v3/sports/football/nfl/athletes/"
            f"{platform_id}",
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        videos = [(v.get("headline") or "", v.get("description") or "")
                  for v in (data.get("videos") or [])[:3]]
        links = (data.get("athlete") or {}).get("links") or []
        player_url = next((l.get("href") for l in links
                           if "playercard" in (l.get("rel") or [])), None)
        return {"videos": videos, "player_url": player_url}

    # --------------------------------------------------------------- writes

    def team_url(self, team: TeamRef) -> str:
        return (f"https://fantasy.espn.com/football/team?leagueId={team.league_id}"
                f"&teamId={team.team_id}&seasonId={self.season}")

    def apply_lineup(self, plan: LineupPlan, week: Optional[int] = None) -> None:
        week = week or self.current_week()
        items = []
        for m in plan.moves:
            from_id = ID_BY_SLOT.get(m.from_slot)
            to_id = ID_BY_SLOT.get(m.to_slot)
            if from_id is None or to_id is None:
                raise ProviderError(
                    f"No ESPN slot id for {m.from_slot.value} -> {m.to_slot.value}")
            items.append({
                "playerId": int(m.player.platform_id),
                "type": "LINEUP",
                "fromLineupSlotId": from_id,
                "toLineupSlotId": to_id,
            })

        body = {
            "isLeagueManager": False,
            "teamId": int(plan.team.team_id),
            "type": "ROSTER",
            "memberId": self.swid,
            "scoringPeriodId": week,
            "executionType": "EXECUTE",
            "items": items,
        }

        override = self._load_capture()
        if override:
            body = _merge_capture(body, override)

        url = (f"{WRITE_HOST}/apis/v3/games/ffl/seasons/{self.season}"
               f"/segments/0/leagues/{plan.team.league_id}/transactions/")
        r = self.session.post(
            url, json=body, timeout=30,
            headers={"Content-Type": "application/json"},
        )
        if r.status_code in (401, 403):
            raise AuthError(
                "ESPN refused the write (%s). Cookies may be stale, or ESPN "
                "changed the endpoint. Run `ff capture espn` to record the real "
                "request from your browser." % r.status_code
            )
        if not r.ok:
            raise ProviderError(
                f"ESPN write failed ({r.status_code}): {r.text[:400]}\n"
                "If this persists, run `ff capture espn` to teach the tool the "
                "current request shape."
            )

    def _load_capture(self) -> Optional[dict]:
        if self.capture_path and self.capture_path.exists():
            try:
                return json.loads(self.capture_path.read_text())
            except json.JSONDecodeError:
                return None
        return None


# ------------------------------------------------------------------ helpers

def _parse_player(entry: dict, week: int,
                  schedule: dict[str, GameInfo]) -> Optional[Player]:
    pool = entry.get("playerPoolEntry", {})
    p = pool.get("player") or entry.get("player")
    if not p:
        return None

    slot = SLOT_BY_ID.get(entry.get("lineupSlotId"), Slot.BENCH)
    pos = POSITION_BY_ID.get(p.get("defaultPositionId"), "UNKNOWN")

    # CAREFUL: ESPN has two unrelated id spaces. `defaultPositionId` is a
    # POSITION id (1=QB, 2=RB, 3=WR, 4=TE, 5=K, 16=DEF). `eligibleSlots` is a
    # list of LINEUP SLOT ids (0=QB, 2=RB, 3=RB/WR, 4=WR, 5=WR/TE, 6=TE,
    # 17=K, 23=FLEX). Reading eligibleSlots through the position table makes
    # every WR look kicker-eligible, because slot 5 (WR/TE) collides with
    # position 5 (K). Resolve slots through SLOT_BY_ID, and let only
    # single-position slots contribute a position -- a WR being FLEX-eligible
    # must not make him RB-eligible.
    eligible = {pos}
    for slot_id in p.get("eligibleSlots", []):
        candidate = SLOT_BY_ID.get(slot_id)
        if candidate in SINGLE_POSITION_SLOTS:
            eligible.add(candidate.value)

    proj = None
    actual = None
    for stat in p.get("stats", []):
        if not (stat.get("scoringPeriodId") == week
                and stat.get("statSplitTypeId") == 1):   # 1 = single week
            continue
        source = stat.get("statSourceId")
        if source == 1:                                  # 1 = projected
            proj = stat.get("appliedTotal")
        elif source == 0:                                 # 0 = actual
            actual = stat.get("appliedTotal")

    abbr = PRO_TEAM_ABBR.get(p.get("proTeamId"), None)
    status_raw = (p.get("injuryStatus") or "ACTIVE").upper()
    availability = INJURY_MAP.get(status_raw, Availability.UNKNOWN)

    game = schedule.get(abbr) if abbr else None
    kickoff = game.kickoff if game else None
    # No game this week for this team means bye.
    if abbr and abbr != "FA" and schedule and abbr not in schedule:
        availability = Availability.BYE

    return Player(
        platform_id=str(p.get("id")),
        name=p.get("fullName") or "Unknown",
        position=pos,
        nfl_team=abbr,
        eligible_positions=eligible or {pos},
        availability=availability,
        injury_note=status_raw if availability is not Availability.ACTIVE else None,
        opponent=game.opponent if game else None,
        game_status=game.status if game else "",
        kickoff=kickoff,
        projection=proj,
        actual_points=actual,
        slot=slot,
    )


def _slot_layout_from_settings(settings: dict) -> list[Slot]:
    counts = settings.get("rosterSettings", {}).get("lineupSlotCounts", {})
    layout: list[Slot] = []
    for slot_id_str, count in sorted(counts.items(), key=lambda kv: int(kv[0])):
        slot = SLOT_BY_ID.get(int(slot_id_str))
        if slot and slot.is_starting:
            layout.extend([slot] * int(count))
    return layout


def _merge_capture(body: dict, capture: dict) -> dict:
    """Overlay a captured request body, keeping our computed items/ids."""
    merged = dict(capture)
    for key in ("teamId", "scoringPeriodId", "items", "memberId"):
        if key in body:
            merged[key] = body[key]
    return merged


def _nfl_week_estimate(season: int) -> int:
    """Rough current-week fallback. Week 1 starts the Tuesday before Labor Day+2."""
    sept1 = datetime(season, 9, 1, tzinfo=timezone.utc)
    # First Thursday on/after Sept 4 is a decent proxy for the season opener.
    opener = sept1 + timedelta(days=(3 - sept1.weekday()) % 7)
    if opener.day < 4:
        opener += timedelta(days=7)
    delta = datetime.now(timezone.utc) - opener
    return max(1, min(18, delta.days // 7 + 1))
