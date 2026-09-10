"""Lineup optimization.

Two jobs, and they are worth keeping separate:

  1. `plan_inactive_swaps` -- the safety net. Only replaces starters who are OUT,
     on IR, suspended, or on bye. Conservative, hard to get wrong, and this is
     what the scheduled auto-pilot runs.

  2. `plan_optimal` -- full projection-based optimization. Suggests real start/sit
     calls. More valuable, more opinionated, and you probably want to eyeball it
     rather than let a cron job commit it.

Locked players (their game has kicked off) are pinned in place and their slots
are removed from the pool. Getting this wrong is the single most expensive bug
in a tool like this, so locks are enforced here rather than trusted to the
platform to reject.
"""
from __future__ import annotations

from typing import Optional

from .models import (SLOT_ELIGIBILITY, Availability, LineupPlan, Move, Player,
                     Roster, Slot, TeamRef)

# Haircut applied to a questionable player's projection. Crude, but it stops the
# optimizer from benching a healthy 11-point player for a questionable 11.2.
QUESTIONABLE_DISCOUNT = 0.85


def _restrictiveness(slot: Slot) -> int:
    """Fewer eligible positions == more restrictive == assign first."""
    return len(SLOT_ELIGIBILITY.get(slot, set()))


def _score(p: Optional[Player]) -> float:
    if p is None:
        return 0.0
    if p.availability.is_dead_weight:
        return 0.0
    proj = p.projection or 0.0
    if p.availability is Availability.QUESTIONABLE:
        return proj * QUESTIONABLE_DISCOUNT
    return proj


def _assign(candidates: list[Player], slots: list[Slot]) -> dict[int, Player]:
    """Greedy most-restrictive-first assignment, then pairwise local search.

    Fantasy slot eligibility is nested (QB subset of SUPERFLEX, RB subset of
    FLEX), so greedy is optimal in the overwhelming majority of real lineups.
    The improvement pass catches the rest. Exact matching would need scipy for
    a difference that shows up roughly never; this is the deliberate trade.
    """
    pool = sorted(candidates, key=_score, reverse=True)
    order = sorted(range(len(slots)), key=lambda i: _restrictiveness(slots[i]))
    assignment: dict[int, Player] = {}
    used: set[str] = set()

    for slot_idx in order:
        slot = slots[slot_idx]
        for p in pool:
            if p.platform_id in used:
                continue
            if p.can_fill(slot):
                assignment[slot_idx] = p
                used.add(p.platform_id)
                break

    # Local search: promote an unassigned player into a slot whenever they beat
    # the incumbent. This is what fixes the case greedy gets wrong -- a flexible
    # high scorer consumed early by a slot a weaker player could have filled.
    #
    # (Swapping two *assigned* players is deliberately not attempted: it leaves
    # the objective unchanged, since both remain in the lineup. Only the set of
    # starters matters, not which legal slot each occupies.)
    improved = True
    guard = 0
    while improved and guard < 50:
        improved = False
        guard += 1
        for i in list(assignment):
            for p in pool:
                if p.platform_id in used or not p.can_fill(slots[i]):
                    continue
                if _score(p) > _score(assignment[i]):
                    used.discard(assignment[i].platform_id)
                    used.add(p.platform_id)
                    assignment[i] = p
                    improved = True
    return assignment


def _build_plan(team: TeamRef, roster: Roster,
                target: dict[int, Player], slots: list[Slot],
                locked_notes: list[str]) -> LineupPlan:
    before = sum(_score(p) for p in roster.starters)

    moves: list[Move] = []
    seen: set[str] = set()
    for idx, player in target.items():
        desired = slots[idx]
        if player.slot != desired:
            moves.append(Move(player=player, from_slot=player.slot, to_slot=desired))
        seen.add(player.platform_id)

    # Anyone currently starting who is not in the target lineup goes to the bench.
    for p in roster.starters:
        if p.platform_id not in seen and not p.is_locked:
            moves.append(Move(player=p, from_slot=p.slot, to_slot=Slot.BENCH))

    after = sum(_score(p) for p in target.values())
    return LineupPlan(
        team=team,
        moves=moves,
        projected_before=round(before, 2),
        projected_after=round(after, 2),
        blocked=locked_notes,
    )


def _split_locked(roster: Roster, slots: list[Slot]) -> tuple[list[Slot], dict[int, Player], list[str]]:
    """Pin locked starters into their slots; return the remaining open slots."""
    open_slots = list(slots)
    pinned: dict[int, Player] = {}
    notes: list[str] = []

    remaining = list(open_slots)
    for p in roster.starters:
        if not p.is_locked:
            continue
        if p.slot in remaining:
            idx = slots.index(p.slot)
            while idx in pinned:
                try:
                    idx = slots.index(p.slot, idx + 1)
                except ValueError:
                    break
            pinned[idx] = p
            remaining.remove(p.slot)
            notes.append(f"{p.name} is locked ({p.slot.value}) - game already started")
    return remaining, pinned, notes


def plan_optimal(team: TeamRef, roster: Roster) -> LineupPlan:
    slots = team.slot_layout or [p.slot for p in roster.starters]
    _, pinned, notes = _split_locked(roster, slots)

    movable = [p for p in roster.players
               if not p.is_locked and p.slot is not Slot.IR]
    taken_idx = set(pinned)
    open_indices = [i for i in range(len(slots)) if i not in taken_idx]
    open_slot_list = [slots[i] for i in open_indices]

    assigned = _assign(movable, open_slot_list)
    target: dict[int, Player] = dict(pinned)
    for local_idx, player in assigned.items():
        target[open_indices[local_idx]] = player

    return _build_plan(team, roster, target, slots, notes)


def plan_inactive_swaps(team: TeamRef, roster: Roster) -> LineupPlan:
    """Only touch starters who are certain zeros. The auto-pilot's remit."""
    slots = team.slot_layout or [p.slot for p in roster.starters]
    _, pinned, notes = _split_locked(roster, slots)

    target: dict[int, Player] = dict(pinned)
    used: set[str] = {p.platform_id for p in pinned.values()}

    bench_pool = sorted(
        [p for p in roster.bench if not p.is_locked
         and not p.availability.is_dead_weight],
        key=_score, reverse=True)

    for idx, slot in enumerate(slots):
        if idx in target:
            continue
        current = next((p for p in roster.starters
                        if p.slot == slot and p.platform_id not in used), None)
        if current is None:
            # Empty slot -- fill it with the best eligible bench player.
            repl = next((b for b in bench_pool
                         if b.platform_id not in used and b.can_fill(slot)), None)
            if repl:
                target[idx] = repl
                used.add(repl.platform_id)
            continue

        used.add(current.platform_id)
        if current.is_locked or not current.availability.is_dead_weight:
            target[idx] = current
            continue

        repl = next((b for b in bench_pool
                     if b.platform_id not in used and b.can_fill(slot)), None)
        if repl and _score(repl) > 0:
            target[idx] = repl
            used.add(repl.platform_id)
            used.discard(current.platform_id)
        else:
            target[idx] = current
            notes.append(
                f"{current.name} is {current.availability.value} but no eligible "
                f"replacement is available for {slot.value}")

    return _build_plan(team, roster, target, slots, notes)


def sub_pairings(roster: Roster, slots: list[Slot]) -> list[tuple[Player, Player]]:
    """For Sleeper AutoSubs: pair each starter with the best eligible backup."""
    pairs: list[tuple[Player, Player]] = []
    used: set[str] = set()
    bench = sorted(roster.bench, key=_score, reverse=True)
    for starter in roster.starters:
        backup = next(
            (b for b in bench
             if b.platform_id not in used
             and b.can_fill(starter.slot)
             and not b.availability.is_dead_weight),
            None)
        if backup:
            pairs.append((starter, backup))
            used.add(backup.platform_id)
    return pairs
