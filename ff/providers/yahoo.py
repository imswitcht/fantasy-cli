"""Yahoo driver -- the only fully sanctioned one.

Yahoo runs a real OAuth 2.0 API with documented write support, so this team can
be automated without reservation: refresh tokens, run on a schedule, and Yahoo
is fine with it. The one carve-out is Yahoo's paid public prize leagues, where
Yahoo blocks third-party lineup submission.

Note that Yahoo also has a built-in "Start Active Players" option that promotes
a healthy bench player over an injured or bye-week starter. If that is all you
want, turn it on in Yahoo and skip the automation. This driver earns its keep by
optimizing on projections, which Yahoo's feature does not do.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import Availability, LineupPlan, Player, Roster, Slot, TeamRef
from .base import AuthError, Capabilities, Provider, ProviderError

SLOT_MAP: dict[str, Slot] = {
    "QB": Slot.QB,
    "RB": Slot.RB,
    "WR": Slot.WR,
    "TE": Slot.TE,
    "K": Slot.K,
    "DEF": Slot.DEF,
    "W/R": Slot.WRRB,
    "W/T": Slot.WRTE,
    "W/R/T": Slot.FLEX,
    "Q/W/R/T": Slot.SUPERFLEX,
    "BN": Slot.BENCH,
    "IR": Slot.IR,
    "IL": Slot.IR,
    "IL+": Slot.IR,
}
YAHOO_BY_SLOT = {v: k for k, v in reversed(list(SLOT_MAP.items()))}

STATUS_MAP = {
    "O": Availability.OUT,
    "D": Availability.DOUBTFUL,
    "Q": Availability.QUESTIONABLE,
    "IR": Availability.INJURED_RESERVE,
    "IR-R": Availability.INJURED_RESERVE,
    "IL": Availability.INJURED_RESERVE,
    "PUP": Availability.OUT,
    "SUSP": Availability.SUSPENDED,
    "NA": Availability.OUT,
    "BYE": Availability.BYE,
}


class YahooProvider(Provider):
    name = "yahoo"

    def __init__(self, token_path: Path, season: int):
        try:
            from yahoo_oauth import OAuth2
            import yahoo_fantasy_api as yfa
        except ImportError as e:  # pragma: no cover
            raise ProviderError(
                "Yahoo support needs two packages: pip install yahoo_fantasy_api yahoo_oauth"
            ) from e

        if not token_path.exists():
            raise AuthError(
                f"No Yahoo token at {token_path}. Run `ff auth yahoo` -- it walks "
                "you through registering an app at developer.yahoo.com and does "
                "the one-time browser consent."
            )
        self._yfa = yfa
        self.season = season
        self.sc = OAuth2(None, None, from_file=str(token_path))
        if not self.sc.token_is_valid():
            self.sc.refresh_access_token()
        self.game = yfa.Game(self.sc, "nfl")
        self._leagues: dict[str, object] = {}

    @property
    def capabilities(self) -> Capabilities:
        return Capabilities(
            can_read=True,
            can_write_lineup=True,
            write_status="official",
            write_caveat=(
                "Official Yahoo API. Note Yahoo blocks third-party lineup writes "
                "on paid public prize leagues."
            ),
        )

    def check_auth(self) -> str:
        try:
            ids = self.game.league_ids(year=self.season)
        except Exception as e:
            raise AuthError(f"Yahoo auth failed: {e}") from e
        return f"Yahoo OK - {len(ids)} league(s) for {self.season}"

    def current_week(self) -> int:
        ids = self.game.league_ids(year=self.season)
        if not ids:
            raise ProviderError(f"No Yahoo leagues found for {self.season}")
        return int(self._league(ids[0]).current_week())

    def _league(self, league_id: str):
        if league_id not in self._leagues:
            self._leagues[league_id] = self.game.to_league(league_id)
        return self._leagues[league_id]

    def discover_teams(self) -> list[TeamRef]:
        refs: list[TeamRef] = []
        for lid in self.game.league_ids(year=self.season):
            lg = self._league(lid)
            settings = lg.settings()
            try:
                team_key = lg.team_key()
            except Exception:
                continue
            layout: list[Slot] = []
            for pos in lg.positions():
                slot = SLOT_MAP.get(pos)
                if slot and slot.is_starting:
                    count = int(lg.positions()[pos].get("count", 1))
                    layout.extend([slot] * count)
            refs.append(TeamRef(
                provider="yahoo",
                league_id=lid,
                team_id=team_key,
                nickname=settings.get("name", "Yahoo league"),
                league_name=settings.get("name"),
                slot_layout=layout,
            ))
        return refs

    def get_roster(self, team: TeamRef, week: Optional[int] = None) -> Roster:
        lg = self._league(team.league_id)
        week = week or int(lg.current_week())
        tm = lg.to_team(team.team_id)

        if not team.slot_layout:
            layout: list[Slot] = []
            positions = lg.positions()
            for pos, meta in positions.items():
                slot = SLOT_MAP.get(pos)
                if slot and slot.is_starting:
                    layout.extend([slot] * int(meta.get("count", 1)))
            team.slot_layout = layout
        if not team.league_name:
            team.league_name = lg.settings().get("name")

        players: list[Player] = []
        for entry in tm.roster(week):
            slot = SLOT_MAP.get(entry.get("selected_position"), Slot.BENCH)
            eligible = {p.upper() for p in entry.get("eligible_positions", [])
                        if p.upper() in {"QB", "RB", "WR", "TE", "K", "DEF"}}
            pos = entry.get("position_type")
            primary = next((p for p in entry.get("eligible_positions", [])
                            if p in {"QB", "RB", "WR", "TE", "K", "DEF"}), "UNKNOWN")
            status = (entry.get("status") or "").upper()
            players.append(Player(
                platform_id=str(entry["player_id"]),
                name=entry.get("name", "Unknown"),
                position=primary,
                eligible_positions=eligible or {primary},
                availability=STATUS_MAP.get(status, Availability.ACTIVE),
                injury_note=status or None,
                slot=slot,
            ))
        return Roster(players=players)

    # --------------------------------------------------------------- writes

    def team_url(self, team: TeamRef) -> str:
        lid = team.league_id.split(".l.")[-1] if ".l." in team.league_id else team.league_id
        return f"https://football.fantasysports.yahoo.com/f1/{lid}"

    def apply_lineup(self, plan: LineupPlan, week: Optional[int] = None) -> None:
        lg = self._league(plan.team.league_id)
        week = week or int(lg.current_week())
        tm = lg.to_team(plan.team.team_id)

        modified = []
        for m in plan.moves:
            yahoo_slot = YAHOO_BY_SLOT.get(m.to_slot)
            if yahoo_slot is None:
                raise ProviderError(f"No Yahoo position string for {m.to_slot.value}")
            modified.append({
                "player_id": int(m.player.platform_id),
                "selected_position": yahoo_slot,
            })

        try:
            tm.change_positions(week, modified)
        except Exception as e:
            msg = str(e)
            if "prize" in msg.lower() or "not allowed" in msg.lower():
                raise ProviderError(
                    "Yahoo refused the write. This is likely a paid public prize "
                    "league, where Yahoo blocks third-party lineup submission. "
                    "You will have to set this one by hand."
                ) from e
            raise ProviderError(f"Yahoo write failed: {e}") from e
