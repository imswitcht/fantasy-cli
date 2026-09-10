"""Interactive Sleeper-style roster browser.

A second, richer way to look at the same data `ff status` already prints --
read-only. Lineup edits stay on the reviewed `ff optimize --apply` /
`ff autopilot --apply` CLI path; see CLAUDE.md's safety rules for why writes
don't belong here too.

Requires `textual`, which is an optional dependency (see requirements.txt) --
`ff tui` in cli.py guards the import so the rest of the tool never needs it.
"""
from __future__ import annotations

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import (Collapsible, Footer, Header, Label,
                             Static, TabbedContent, TabPane)

from .config import Config, ProviderRegistry
from .display import avail_style, display_key, enrich
from .models import Player, Roster, TeamRef

POSITION_CSS_CLASS = {
    "QB": "pos-qb", "RB": "pos-rb", "WR": "pos-wr",
    "TE": "pos-te", "K": "pos-k", "DEF": "pos-def",
}


class PlayerRow(Horizontal):
    """One player: position badge, name/team, opponent, projection, status."""

    def __init__(self, player: Player) -> None:
        self._player = player
        super().__init__(classes="player-row")

    def compose(self) -> ComposeResult:
        p = self._player
        badge_cls = POSITION_CSS_CLASS.get(p.position, "pos-def")
        lock = " \U0001F512" if p.is_locked else ""
        proj = f"{p.projection:.1f}" if p.projection is not None else "-"
        opp = f"@{p.opponent}" if p.opponent else ""
        yield Label(p.position, classes=f"badge {badge_cls}")
        yield Label(f"{p.name}{lock}", classes="player-name")
        yield Label(f"{p.nfl_team or '-'} {opp}".strip(), classes="player-meta")
        yield Label(proj, classes="player-proj")
        yield Static(avail_style(p), classes="player-status")


class RosterView(VerticalScroll):
    """Everything for one team: summary line, starters, collapsible bench."""

    def __init__(self, team: TeamRef, reg: ProviderRegistry) -> None:
        self._team = team
        self._reg = reg
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Label(f"[dim]Loading {self._team.nickname}...[/dim]",
                    classes="summary", id="summary")
        yield Vertical(id="starters")
        yield Collapsible(Vertical(id="bench-rows"), title="BENCH",
                          collapsed=True, id="bench")

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
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))
            return
        self.app.call_from_thread(self._render_roster, roster, wk)

    def _show_error(self, msg: str) -> None:
        self.query_one("#summary", Label).update(
            f"[red]{self._team.nickname}: {msg}[/red]")

    def _render_roster(self, roster: Roster, week: int) -> None:
        t = self._team
        self.query_one("#summary", Label).update(
            f"[bold]{t.nickname}[/bold]  [dim]{t.provider} · week {week} · "
            f"projected {roster.projected_total:.1f}[/dim]")

        starters_box = self.query_one("#starters", Vertical)
        starters_box.remove_children()
        for p in sorted(roster.starters, key=display_key):
            starters_box.mount(PlayerRow(p))

        bench_players = sorted(roster.bench + roster.injured_reserve, key=display_key)
        bench_box = self.query_one("#bench-rows", Vertical)
        bench_box.remove_children()
        for p in bench_players:
            bench_box.mount(PlayerRow(p))
        self.query_one("#bench", Collapsible).title = f"BENCH ({len(bench_players)})"


class FantasyTUI(App):
    """Sleeper-styled, read-only roster browser."""

    CSS_PATH = "tui.tcss"
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("r", "refresh_active", "Refresh"),
        ("b", "toggle_bench", "Toggle bench"),
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

    def action_toggle_bench(self) -> None:
        tabs = self.query(TabbedContent)
        if not tabs:
            return
        pane = tabs.first().active_pane
        if pane is not None:
            collapsible = pane.query_one(Collapsible)
            collapsible.collapsed = not collapsible.collapsed
