"""Provider interface.

Every platform driver implements this. The important part is `capabilities` --
the app never pretends a platform can do something it cannot. Where a write is
unsupported or unverified, the CLI degrades to "here is the change, go make it"
rather than silently failing.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..models import LineupPlan, Roster, TeamRef


class ProviderError(RuntimeError):
    """Anything that went wrong talking to a platform."""


class NotSupported(ProviderError):
    """This platform cannot do this operation at all."""


class AuthError(ProviderError):
    """Credentials missing, expired, or rejected."""


@dataclass(frozen=True)
class Capabilities:
    can_read: bool
    can_write_lineup: bool
    # "official"   - documented, sanctioned API. Safe to automate.
    # "unofficial" - reverse-engineered. Works until it doesn't.
    # "none"       - no write path; we deep-link the user instead.
    write_status: str
    # Shown to the user before any write is committed.
    write_caveat: Optional[str] = None


class Provider(ABC):
    name: str = "base"

    @property
    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    def check_auth(self) -> str:
        """Prove credentials work. Return a short human-readable identity string.

        Raises AuthError with an actionable message if not.
        """

    @abstractmethod
    def current_week(self) -> int: ...

    @abstractmethod
    def get_roster(self, team: TeamRef, week: Optional[int] = None) -> Roster: ...

    @abstractmethod
    def discover_teams(self) -> list[TeamRef]:
        """Best-effort listing of the user's teams, for first-run config."""

    def apply_lineup(self, plan: LineupPlan, week: Optional[int] = None) -> None:
        """Commit a lineup plan. Default: refuse loudly."""
        raise NotSupported(
            f"{self.name} has no supported write path. "
            f"Make these changes in the {self.name} app:\n"
            + "\n".join(f"  - {m}" for m in plan.moves)
        )

    def team_url(self, team: TeamRef) -> str:
        """Deep link to the team page, used when we can't write."""
        return ""
