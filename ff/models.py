"""Canonical domain model shared by every provider.

Everything a provider returns gets normalized into these types, so the
optimizer and CLI never need to know which platform a team came from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Slot(str, Enum):
    """Canonical lineup slots. Platform-specific slots map onto these."""

    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DEF = "DEF"
    FLEX = "FLEX"            # RB / WR / TE
    WRRB = "WRRB"            # RB / WR
    WRTE = "WRTE"            # WR / TE
    SUPERFLEX = "SUPERFLEX"  # QB / RB / WR / TE
    BENCH = "BENCH"
    IR = "IR"

    @property
    def is_starting(self) -> bool:
        return self not in (Slot.BENCH, Slot.IR)


# Which positions may legally occupy each slot.
SLOT_ELIGIBILITY: dict[Slot, set[str]] = {
    Slot.QB: {"QB"},
    Slot.RB: {"RB"},
    Slot.WR: {"WR"},
    Slot.TE: {"TE"},
    Slot.K: {"K"},
    Slot.DEF: {"DEF"},
    Slot.FLEX: {"RB", "WR", "TE"},
    Slot.WRRB: {"RB", "WR"},
    Slot.WRTE: {"WR", "TE"},
    Slot.SUPERFLEX: {"QB", "RB", "WR", "TE"},
    Slot.BENCH: {"QB", "RB", "WR", "TE", "K", "DEF"},
    Slot.IR: {"QB", "RB", "WR", "TE", "K", "DEF"},
}


class Availability(str, Enum):
    """How likely this player is to actually record a stat line this week."""

    ACTIVE = "ACTIVE"
    QUESTIONABLE = "QUESTIONABLE"
    DOUBTFUL = "DOUBTFUL"
    OUT = "OUT"
    INJURED_RESERVE = "IR"
    SUSPENDED = "SUSPENDED"
    BYE = "BYE"
    UNKNOWN = "UNKNOWN"

    @property
    def is_dead_weight(self) -> bool:
        """True when starting this player is very likely to score zero."""
        return self in (
            Availability.OUT,
            Availability.DOUBTFUL,
            Availability.INJURED_RESERVE,
            Availability.SUSPENDED,
            Availability.BYE,
        )


@dataclass
class Player:
    # Identity
    platform_id: str                 # the id this platform uses
    name: str
    position: str                    # primary position, normalized (QB/RB/WR/TE/K/DEF)
    canonical_id: Optional[str] = None  # cross-platform id, filled by the crosswalk
    nfl_team: Optional[str] = None

    # Week context
    eligible_positions: set[str] = field(default_factory=set)
    availability: Availability = Availability.UNKNOWN
    injury_note: Optional[str] = None
    opponent: Optional[str] = None
    kickoff: Optional[datetime] = None   # tz-aware UTC
    projection: Optional[float] = None

    # Where the player currently sits
    slot: Slot = Slot.BENCH

    def __post_init__(self) -> None:
        if not self.eligible_positions:
            self.eligible_positions = {self.position}

    @property
    def is_locked(self) -> bool:
        """True once this player's game has kicked off — cannot be moved."""
        if self.kickoff is None:
            return False
        return datetime.now(timezone.utc) >= self.kickoff

    def can_fill(self, slot: Slot) -> bool:
        allowed = SLOT_ELIGIBILITY.get(slot, set())
        return bool(self.eligible_positions & allowed)

    @property
    def label(self) -> str:
        bits = [self.name, self.position]
        if self.nfl_team:
            bits.append(self.nfl_team)
        return f"{' '.join(bits)}"

    def __repr__(self) -> str:  # pragma: no cover - debug convenience
        return f"<Player {self.name} {self.position} {self.slot.value}>"


@dataclass
class Roster:
    players: list[Player]

    @property
    def starters(self) -> list[Player]:
        return [p for p in self.players if p.slot.is_starting]

    @property
    def bench(self) -> list[Player]:
        return [p for p in self.players if p.slot is Slot.BENCH]

    @property
    def injured_reserve(self) -> list[Player]:
        return [p for p in self.players if p.slot is Slot.IR]

    def by_platform_id(self, pid: str) -> Optional[Player]:
        return next((p for p in self.players if p.platform_id == str(pid)), None)

    @property
    def projected_total(self) -> float:
        """Expected points from the current starters.

        Players who are OUT / IR / suspended / on bye count as zero rather than
        their raw projection, so this number matches what the optimizer compares
        against instead of flattering a lineup full of inactives.
        """
        return sum(0.0 if p.availability.is_dead_weight else (p.projection or 0.0)
                   for p in self.starters)


@dataclass
class TeamRef:
    """Enough to address one of the user's teams on one platform."""

    provider: str          # "yahoo" | "espn" | "sleeper"
    league_id: str
    team_id: str
    nickname: str          # what the user calls it, e.g. "Work league"
    league_name: Optional[str] = None
    # Ordered starting slots for this league, e.g. [QB, RB, RB, WR, WR, TE, FLEX, K, DEF]
    slot_layout: list[Slot] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.league_id}:{self.team_id}"


@dataclass
class Move:
    """One player changing slots. The unit of a lineup diff."""

    player: Player
    from_slot: Slot
    to_slot: Slot

    def __str__(self) -> str:
        return f"{self.player.name}: {self.from_slot.value} -> {self.to_slot.value}"


@dataclass
class LineupPlan:
    team: TeamRef
    moves: list[Move]
    projected_before: float
    projected_after: float
    blocked: list[str] = field(default_factory=list)  # human-readable reasons

    @property
    def gain(self) -> float:
        return self.projected_after - self.projected_before

    @property
    def is_noop(self) -> bool:
        return not self.moves
