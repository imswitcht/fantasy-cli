"""Presentation helpers shared by the CLI tables and the TUI.

Both front ends show the same underlying roster data (see ff/models.py) and
need to agree on how it's ordered, colored, and enriched with projections --
this is that shared logic, kept independent of which widget toolkit is
rendering it.
"""
from __future__ import annotations

from .config import ProviderRegistry
from .crosswalk import Crosswalk, attach_projections
from .models import Availability, Player, Roster, Slot
from .ui import Text, console

WRITE_BADGE = {
    "official": ("[green]official API[/green]", "✔"),
    "unofficial": ("[yellow]unofficial[/yellow]", "~"),
    "none": ("[red]read-only[/red]", "✘"),
}


def enrich(reg: ProviderRegistry, provider_name: str,
           roster: Roster, week: int, quiet: bool = True) -> None:
    """Attach shared Sleeper projections so all four teams are comparable."""
    sl = reg.sleeper_for_data
    if sl is None:
        return
    try:
        cw = Crosswalk.from_sleeper_catalog(sl.players())
        proj = sl.projections(week)
    except Exception as e:
        if not quiet:
            console.print(f"[yellow]Projections unavailable ({e}). "
                          "Falling back to platform projections.[/yellow]")
        return
    matched, misses = attach_projections(roster.players, provider_name, cw, proj)
    if misses and not quiet:
        console.print(f"[dim]No projection matched for: {', '.join(misses[:6])}"
                      f"{' ...' if len(misses) > 6 else ''}[/dim]")


AVAILABILITY_COLORS = {
    Availability.OUT: "red",
    Availability.INJURED_RESERVE: "red",
    Availability.SUSPENDED: "red",
    Availability.BYE: "red",
    Availability.DOUBTFUL: "red",
    Availability.QUESTIONABLE: "yellow",
}


def avail_style(p: Player) -> Text:
    if p.availability is Availability.ACTIVE:
        return Text("")
    return Text(p.availability.value,
                style=AVAILABILITY_COLORS.get(p.availability, "dim"))


# Platforms return roster entries in their own internal order, which reads as
# random. Sort into the order people actually think about a lineup in.
SLOT_DISPLAY_ORDER = {
    slot: i for i, slot in enumerate([
        Slot.QB, Slot.RB, Slot.WR, Slot.TE,
        Slot.FLEX, Slot.WRRB, Slot.WRTE, Slot.SUPERFLEX,
        Slot.K, Slot.DEF, Slot.BENCH, Slot.IR,
    ])
}
POS_DISPLAY_ORDER = {p: i for i, p in enumerate(
    ["QB", "RB", "WR", "TE", "K", "DEF"])}


def display_key(p: Player):
    return (
        SLOT_DISPLAY_ORDER.get(p.slot, 99),
        POS_DISPLAY_ORDER.get(p.position, 99),
        -(p.projection or 0.0),
        p.name,
    )
