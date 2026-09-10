"""The safe write path.

Every commit follows the same four steps, and the last one is the one most
tools skip:

    1. Re-read the roster immediately before writing (state may have moved since
       the plan was computed -- a player got ruled out, a game kicked off).
    2. Recompute the plan against that fresh state and abort if it changed.
    3. Submit.
    4. Re-read again and verify the lineup actually matches what we asked for.

Step 4 exists because ESPN in particular will happily return 200 on a write it
did not apply. A tool that trusts the status code will tell you your lineup is
fixed when it isn't, which is worse than doing nothing at all.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .models import LineupPlan, Roster, Slot, TeamRef
from .providers.base import NotSupported, Provider, ProviderError


class WriteRefused(RuntimeError):
    """We declined to write. Not an API failure -- a safety stop."""


class VerificationFailed(RuntimeError):
    """The write was accepted but the roster does not reflect it."""


def commit(provider: Provider,
           team: TeamRef,
           planner: Callable[[TeamRef, Roster], LineupPlan],
           week: Optional[int],
           log_path: Optional[Path] = None,
           min_gain: float = 0.0) -> LineupPlan:
    caps = provider.capabilities
    if not caps.can_write_lineup:
        raise NotSupported(
            f"{provider.name} has no write path -- "
            f"{caps.write_caveat or 'not supported'}")

    # 1 + 2: re-read and recompute against fresh state.
    fresh_roster = provider.get_roster(team, week)
    fresh_plan = planner(team, fresh_roster)

    if fresh_plan.is_noop:
        raise WriteRefused("Nothing to change -- lineup is already correct.")

    if fresh_plan.gain < min_gain:
        raise WriteRefused(
            f"Projected gain {fresh_plan.gain:+.2f} is below the "
            f"min_gain_to_write threshold of {min_gain:.2f}.")

    locked_conflict = [m for m in fresh_plan.moves if m.player.is_locked]
    if locked_conflict:
        raise WriteRefused(
            "Plan touches locked players (games already started): "
            + ", ".join(m.player.name for m in locked_conflict))

    before_snapshot = _snapshot(fresh_roster)

    # 3: submit.
    provider.apply_lineup(fresh_plan, week)

    # 4: verify.
    after_roster = provider.get_roster(team, week)
    after_snapshot = _snapshot(after_roster)
    unapplied = []
    for m in fresh_plan.moves:
        actual = after_snapshot.get(m.player.platform_id)
        if actual != m.to_slot.value:
            unapplied.append(
                f"{m.player.name}: wanted {m.to_slot.value}, is {actual or 'missing'}")

    _log(log_path, team, fresh_plan, before_snapshot, after_snapshot, unapplied)

    if unapplied:
        raise VerificationFailed(
            f"{provider.name} accepted the request but the roster did not change "
            "as requested:\n  " + "\n  ".join(unapplied)
            + "\n\nYour lineup may be partially updated. Check it in the app."
        )
    return fresh_plan


def _snapshot(roster: Roster) -> dict[str, str]:
    return {p.platform_id: p.slot.value for p in roster.players}


def _log(path: Optional[Path], team: TeamRef, plan: LineupPlan,
         before: dict, after: dict, unapplied: list[str]) -> None:
    if path is None:
        return
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "team": team.key,
        "nickname": team.nickname,
        "moves": [str(m) for m in plan.moves],
        "projected_before": plan.projected_before,
        "projected_after": plan.projected_after,
        "verified": not unapplied,
        "unapplied": unapplied,
        "roster_before": before,
        "roster_after": after,
    }
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
