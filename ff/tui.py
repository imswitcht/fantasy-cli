"""Interactive Sleeper-style roster browser.

A second, richer way to look at the same data `ff status` already prints.
Mostly read-only, but on platforms where `provider.capabilities.can_write_lineup`
is true (ESPN today; Sleeper's is False by design, Yahoo's provider can't
authenticate yet -- see CLAUDE.md's platform table) you can click a starter
and a bench player to swap them. That goes through the exact same
read -> recompute -> submit -> verify path as `ff optimize --apply`
(`ff/writer.py:commit`), gated behind a confirm dialog before anything is
sent -- nothing here bypasses the project's normal write safety rules.

Requires `textual`, which is an optional dependency (see requirements.txt) --
`ff tui` in cli.py guards the import so the rest of the tool never needs it.
"""
from __future__ import annotations

from typing import Optional

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, Footer, Header, Label, Static,
                             TabbedContent, TabPane)

from .config import Config, LOG_PATH, ProviderRegistry
from .display import avail_style, display_key, enrich
from .models import LineupPlan, Move, Player, Roster, TeamRef
from .optimizer import validate_plan
from .providers.base import NotSupported, ProviderError
from .writer import VerificationFailed, WriteRefused, commit

POSITION_CSS_CLASS = {
    "QB": "pos-qb", "RB": "pos-rb", "WR": "pos-wr",
    "TE": "pos-te", "K": "pos-k", "DEF": "pos-def",
}


def _swap_planner(pid_a: str, pid_b: str):
    """Builds a `commit()`-compatible planner for a manual two-player swap.

    Re-locates both players by platform id against whatever roster `commit()`
    re-reads at write time, rather than closing over stale Player objects --
    the whole point of commit()'s recompute step is to catch state that moved
    between when the user clicked and when the write actually happens.
    """
    def planner(team: TeamRef, roster: Roster) -> LineupPlan:
        a = roster.by_platform_id(pid_a)
        b = roster.by_platform_id(pid_b)
        if a is None or b is None:
            raise WriteRefused("A selected player is no longer on the roster.")
        if a.is_locked or b.is_locked:
            raise WriteRefused("A selected player's game has already started.")

        def contrib(p, slot):
            if not slot.is_starting or p.availability.is_dead_weight:
                return 0.0
            return p.projection or 0.0

        before = roster.projected_total
        after = (before - contrib(a, a.slot) - contrib(b, b.slot)
                        + contrib(a, b.slot) + contrib(b, a.slot))

        plan = LineupPlan(
            team=team,
            moves=[Move(a, a.slot, b.slot), Move(b, b.slot, a.slot)],
            projected_before=round(before, 2),
            projected_after=round(after, 2),
        )
        validate_plan(plan)
        return plan
    return planner


class PlayerRow(Horizontal):
    """One player: position badge, name/team, opponent, projection, status."""

    class Clicked(Message):
        def __init__(self, row: "PlayerRow") -> None:
            self.row = row
            super().__init__()

    def __init__(self, player: Player, writable: bool = False) -> None:
        self.player = player
        self.writable = writable and not player.is_locked
        classes = "player-row" + (" writable" if self.writable else "")
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:
        p = self.player
        badge_cls = POSITION_CSS_CLASS.get(p.position, "pos-def")
        lock = " \U0001F512" if p.is_locked else ""
        proj = f"{p.projection:.1f}" if p.projection is not None else "-"
        opp = f"@{p.opponent}" if p.opponent else ""
        yield Label(p.position, classes=f"badge {badge_cls}")
        yield Label(f"{p.name}{lock}", classes="player-name")
        yield Label(f"{p.nfl_team or '-'} {opp}".strip(), classes="player-meta")
        yield Label(proj, classes="player-proj")
        yield Static(avail_style(p), classes="player-status")

    def on_click(self, event: events.Click) -> None:
        if self.writable:
            self.post_message(self.Clicked(self))


class SwapConfirmScreen(ModalScreen[bool]):
    """Confirm dialog shown before a click-to-swap write is submitted."""

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, team: TeamRef, a: Player, b: Player,
                 before: float, after: float) -> None:
        self._team = team
        self._a = a
        self._b = b
        self._before = before
        self._after = after
        super().__init__()

    def compose(self) -> ComposeResult:
        gain = self._after - self._before
        with Vertical(id="swap-dialog"):
            yield Label(f"[bold]{self._team.nickname}[/bold]", classes="swap-line")
            yield Label(
                f"{self._a.name} ({self._a.slot.value}) ↔ "
                f"{self._b.name} ({self._b.slot.value})",
                classes="swap-line")
            yield Label(
                f"Projected: {self._before:.1f} → {self._after:.1f} "
                f"({gain:+.1f})", classes="swap-line")
            with Horizontal(id="swap-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="confirm", variant="primary")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")

    def action_cancel(self) -> None:
        self.dismiss(False)


class RosterView(VerticalScroll):
    """Everything for one team: summary line, starters, bench -- one page."""

    def __init__(self, team: TeamRef, reg: ProviderRegistry) -> None:
        self._team = team
        self._reg = reg
        self._writable = False
        self._week: Optional[int] = None
        self._roster: Optional[Roster] = None
        self._selected: Optional[PlayerRow] = None
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label(f"[dim]Loading {self._team.nickname}...[/dim]",
                    classes="summary", id="summary")
        yield Vertical(id="starters")
        yield Label("BENCH", classes="section-label", id="bench-label")
        yield Vertical(id="bench-rows")

    def on_mount(self) -> None:
        self.load_roster()

    @work(exclusive=True, thread=True)
    def load_roster(self) -> None:
        t = self._team
        try:
            prov, err = self._reg.try_get(t.provider)
            if prov is None:
                raise RuntimeError(err or "unavailable")
            wk = prov.current_week()
            roster = prov.get_roster(t, wk)
            enrich(self._reg, t.provider, roster, wk)
            writable = prov.capabilities.can_write_lineup
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))
            return
        self.app.call_from_thread(self._render_roster, roster, wk, writable)

    def _show_error(self, msg: str) -> None:
        self.query_one("#summary", Label).update(
            f"[red]{self._team.nickname}: {msg}[/red]")

    def _render_roster(self, roster: Roster, week: int, writable: bool) -> None:
        t = self._team
        self._week = week
        self._roster = roster
        self._writable = writable
        self._selected = None
        self.query_one("#summary", Label).update(
            f"[bold]{t.nickname}[/bold]  [dim]{t.provider} · week {week} · "
            f"projected {roster.projected_total:.1f}[/dim]")

        starters_box = self.query_one("#starters", Vertical)
        starters_box.remove_children()
        for p in sorted(roster.starters, key=display_key):
            starters_box.mount(PlayerRow(p, writable=writable))

        bench_players = sorted(roster.bench + roster.injured_reserve, key=display_key)
        bench_box = self.query_one("#bench-rows", Vertical)
        bench_box.remove_children()
        for p in bench_players:
            bench_box.mount(PlayerRow(p, writable=writable))
        self.query_one("#bench-label", Label).update(f"BENCH ({len(bench_players)})")

    # ------------------------------------------------------------ swapping

    def on_player_row_clicked(self, message: PlayerRow.Clicked) -> None:
        row = message.row
        if self._selected is None:
            self._selected = row
            row.add_class("selected")
            return

        if row is self._selected:
            row.remove_class("selected")
            self._selected = None
            return

        a_row, b_row = self._selected, row
        self._selected.remove_class("selected")
        self._selected = None

        a, b = a_row.player, b_row.player
        if not (a.can_fill(b.slot) and b.can_fill(a.slot)):
            self.app.notify(
                f"{a.name} and {b.name} aren't eligible to swap slots.",
                severity="error")
            return

        self.run_worker(self._confirm_and_swap(a, b), exclusive=False)

    async def _confirm_and_swap(self, a: Player, b: Player) -> None:
        # Mirrors _swap_planner's math, purely for the confirm dialog -- the
        # planner recomputes for real against whatever commit() re-reads.
        def contrib(p, slot):
            if not slot.is_starting or p.availability.is_dead_weight:
                return 0.0
            return p.projection or 0.0

        before = self._roster.projected_total
        after = (before - contrib(a, a.slot) - contrib(b, b.slot)
                        + contrib(a, b.slot) + contrib(b, a.slot))

        confirmed = await self.app.push_screen_wait(
            SwapConfirmScreen(self._team, a, b, before, after))
        if not confirmed:
            return
        self.submit_swap(a.platform_id, b.platform_id)

    @work(exclusive=True, thread=True)
    def submit_swap(self, pid_a: str, pid_b: str) -> None:
        t = self._team
        prov, err = self._reg.try_get(t.provider)
        if prov is None:
            self.app.call_from_thread(
                self.app.notify, err or "Provider unavailable", severity="error")
            return
        try:
            commit(prov, t, _swap_planner(pid_a, pid_b), self._week,
                   log_path=LOG_PATH)
        except (WriteRefused, VerificationFailed, NotSupported, ProviderError) as e:
            self.app.call_from_thread(self.app.notify, str(e), severity="error")
            return
        except Exception as e:  # pragma: no cover - unexpected provider failure
            self.app.call_from_thread(
                self.app.notify, f"Swap failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, "Lineup updated.")
        self.app.call_from_thread(self.load_roster)


class FantasyTUI(App):
    """Sleeper-styled roster browser, with click-to-swap where writes are safe."""

    CSS_PATH = "tui.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_active", "Refresh"),
    ]
    TITLE = "ff tui"

    def __init__(self) -> None:
        super().__init__()
        self.cfg = Config.load()
        self.reg = ProviderRegistry(self.cfg)

    def compose(self) -> ComposeResult:
        yield Header()
        if not self.cfg.teams:
            yield Label("No teams configured. Edit config.toml and restart.")
        else:
            with TabbedContent():
                for i, t in enumerate(self.cfg.teams):
                    with TabPane(f"{t.nickname} ({t.provider})", id=f"team-{i}"):
                        yield RosterView(t, self.reg)
        yield Footer()

    def action_refresh_active(self) -> None:
        tabs = self.query(TabbedContent)
        if not tabs:
            return
        pane = tabs.first().active_pane
        if pane is not None:
            pane.query_one(RosterView).load_roster()
