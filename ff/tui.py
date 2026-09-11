"""Interactive Sleeper-style roster browser.

A second, richer way to look at the same data `ff status` already prints.
Mostly read-only, but on platforms where `provider.capabilities.can_write_lineup`
is true (ESPN today; Sleeper's is False by design, Yahoo's provider can't
authenticate yet -- see CLAUDE.md's platform table) each writable row has a
small Move button that starts a swap: pick a starter and a bench player and
it goes through the exact same read -> recompute -> submit -> verify path as
`ff optimize --apply` (`ff/writer.py:commit`), gated behind a confirm dialog
before anything is sent -- nothing here bypasses the project's normal write
safety rules. A manual swap is always exempt from `min_gain_to_write` (the
user already approved that exact move by clicking Confirm); the writable
teams also get an "Auto-optimization" switch (default ON) that gates whether
`ff optimize --apply` / `ff autopilot --apply` may write to *that team* at
all -- OFF skips those commands for the team entirely, leaving it solely to
manual click-to-swap, which is unaffected either way.

Clicking a player's *name* (any platform, not just writable ones) opens a
read-only info panel. ESPN players get a bit more there -- ESPN's public
per-athlete endpoint has real player-specific "videos" (short clips with
real headlines, e.g. fantasy-relevant commentary) and a verified player-card
link (`EspnProvider.player_info`); ESPN's per-player *news* filter turned out
not to work at all when tested (the `athlete=` param is silently ignored),
so that's not used. Sleeper and Yahoo have no player-news/clips capability in
their public APIs, so their panel only shows the bio fields already on the
`Player` object -- no fabricated news section, no guessed deep link.

Requires `textual`, which is an optional dependency (see requirements.txt) --
`ff tui` in cli.py guards the import so the rest of the tool never needs it.
"""
from __future__ import annotations

import webbrowser
from typing import Optional

from textual import events, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import (Button, Footer, Header, Label, Select, Static,
                             Switch, TabbedContent, TabPane)

from .config import (Config, LOG_PATH, ProviderRegistry,
                     load_auto_optimize_overrides, set_auto_optimize_override)
from .display import avail_style, display_key, enrich
from .models import LineupPlan, MatchupSummary, Move, Player, Roster, TeamRef
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


class PlayerNameLabel(Static):
    """The player's name, clickable on every platform to open the info panel."""

    def __init__(self, player: Player) -> None:
        self._player = player
        lock = " \U0001F512" if player.is_locked else ""
        super().__init__(f"{player.name}{lock}", classes="player-name clickable-name")

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(PlayerRow.NameClicked(self._player))


class PlayerRow(Horizontal):
    """One player: badge, clickable name, team/opponent/game, proj/actual, status, move."""

    class Clicked(Message):
        """Move button pressed -- starts/continues a swap selection."""
        def __init__(self, row: "PlayerRow") -> None:
            self.row = row
            super().__init__()

    class NameClicked(Message):
        """Player name clicked -- opens the read-only info panel."""
        def __init__(self, player: Player) -> None:
            self.player = player
            super().__init__()

    def __init__(self, player: Player, writable: bool = False) -> None:
        self.player = player
        self.writable = writable and not player.is_locked
        classes = "player-row" + (" writable" if self.writable else "")
        super().__init__(classes=classes)

    def compose(self) -> ComposeResult:
        p = self.player
        badge_cls = POSITION_CSS_CLASS.get(p.position, "pos-def")
        proj = f"{p.projection:.1f}" if p.projection is not None else "-"
        actual = f"{p.actual_points:.1f}" if p.actual_points is not None else "-"
        opp = f"@{p.opponent}" if p.opponent else "-"
        if p.game_score:
            status = f"{p.game_score} {p.game_status}".strip()
        else:
            status = p.game_status or "-"
        yield Label(p.position, classes=f"badge {badge_cls}")
        yield PlayerNameLabel(p)
        yield Label(p.nfl_team or "-", classes="player-team")
        yield Label(opp, classes="player-opp")
        yield Label(status, classes="player-game")
        yield Label(proj, classes="player-proj")
        yield Label(actual, classes="player-actual")
        yield Static(avail_style(p), classes="player-avail")
        if self.writable:
            yield Button("⇄", id="move", classes="move-btn")
        else:
            yield Static("", classes="move-btn-spacer")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "move":
            event.stop()
            self.post_message(self.Clicked(self))


class ColumnHeader(Horizontal):
    """Column labels for a RosterView's player rows -- widths must match PlayerRow's."""

    def compose(self) -> ComposeResult:
        yield Label("", classes="badge header-cell")
        yield Label("Player", classes="player-name header-cell")
        yield Label("Team", classes="player-team header-cell")
        yield Label("Opp", classes="player-opp header-cell")
        yield Label("Game", classes="player-game header-cell")
        yield Label("Proj", classes="player-proj header-cell")
        yield Label("Pts", classes="player-actual header-cell")
        yield Label("Status", classes="player-avail header-cell")
        yield Label("", classes="move-btn-spacer header-cell")


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


class NewsItemLabel(Static):
    """One clickable entry in the info panel: a clip/news headline or the
    player-card link. Clicking opens the real URL in the system browser --
    nothing here is simulated or guessed at, so a missing url just means no
    click handler rather than a dead link."""

    def __init__(self, headline: str, description: str = "",
                url: Optional[str] = None) -> None:
        self._url = url
        text = f"• {headline}"
        if description and description != headline:
            text += f"\n  [dim]{description}[/dim]"
        classes = "news-item" + (" clickable-news" if url else "")
        super().__init__(text, classes=classes)

    def on_click(self, event: events.Click) -> None:
        if not self._url:
            return
        event.stop()
        try:
            webbrowser.open(self._url)
            self.app.notify("Opened in browser.")
        except Exception as e:
            self.app.notify(f"Couldn't open browser: {e}", severity="error")


class PlayerInfoScreen(ModalScreen):
    """Read-only player info: bio always, ESPN clips + player-card link when available."""

    BINDINGS = [("escape", "close", "Close")]

    def __init__(self, player: Player, provider_name: str, reg: ProviderRegistry) -> None:
        self._player = player
        self._provider_name = provider_name
        self._reg = reg
        super().__init__()

    def compose(self) -> ComposeResult:
        p = self._player
        matchup = f"{'@' if p.opponent else ''}{p.opponent or 'no game data'}"
        if p.game_status:
            matchup += f"  ({p.game_status})"
        with Vertical(id="info-dialog"):
            yield Label(f"[bold]{p.name}[/bold]  {p.position} · {p.nfl_team or '-'}",
                       classes="info-line")
            yield Label(f"Status: {p.injury_note or p.availability.value}",
                       classes="info-line")
            yield Label(f"Matchup: {matchup}", classes="info-line")
            proj = f"{p.projection:.1f}" if p.projection is not None else "-"
            actual = f"{p.actual_points:.1f}" if p.actual_points is not None else "-"
            yield Label(f"Projected: {proj}  ·  Scored: {actual}", classes="info-line")
            yield Vertical(id="info-extra")
            with Horizontal(id="info-buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        extra = self.query_one("#info-extra", Vertical)
        if self._provider_name == "espn":
            extra.mount(Label("[dim]Loading ESPN clips...[/dim]", classes="news-item"))
            self.load_espn_extra()
        else:
            extra.mount(Label("[dim]No news/clips feed available from this platform.[/dim]",
                              classes="news-item"))

    @work(thread=True)
    def load_espn_extra(self) -> None:
        prov, err = self._reg.try_get("espn")
        if prov is None or not hasattr(prov, "player_info"):
            self.app.call_from_thread(self._show_error, err or "ESPN unavailable")
            return
        try:
            info = prov.player_info(self._player.platform_id)
        except Exception as e:
            self.app.call_from_thread(self._show_error, f"Couldn't load ESPN info: {e}")
            return
        self.app.call_from_thread(self._render_extra, info)

    def _show_error(self, msg: str) -> None:
        try:
            extra = self.query_one("#info-extra", Vertical)
        except Exception:
            return  # dialog may already be closed
        extra.remove_children()
        extra.mount(Label(f"[red]{msg}[/red]", classes="news-item"))

    def _render_extra(self, info: dict) -> None:
        try:
            extra = self.query_one("#info-extra", Vertical)
        except Exception:
            return  # dialog may already be closed
        extra.remove_children()
        items = info.get("items") or []
        if not items:
            extra.mount(Label("[dim]No recent ESPN clips.[/dim]", classes="news-item"))
        for item in items:
            extra.mount(NewsItemLabel(item["headline"], item.get("description", ""),
                                      item.get("url")))
        if info.get("player_url"):
            extra.mount(NewsItemLabel("View full player page on ESPN.com",
                                      url=info["player_url"]))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss()

    def action_close(self) -> None:
        self.dismiss()


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
        yield ColumnHeader(classes="column-header")
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

        if writable and not self.query("#controls"):
            enabled = load_auto_optimize_overrides().get(t.key, True)
            self.mount(
                Horizontal(
                    Label("Auto-optimization", classes="section-label"),
                    Switch(value=enabled, id="auto-optimize-switch"),
                    id="controls",
                ),
                after=self.query_one("#summary"),
            )

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

    def on_switch_changed(self, message: Switch.Changed) -> None:
        if message.switch.id != "auto-optimize-switch":
            return
        set_auto_optimize_override(self._team.key, message.value)
        if message.value:
            self.app.notify(f"Auto-optimization ON for {self._team.nickname} "
                            "-- ff optimize/autopilot --apply may write to this team.")
        else:
            self.app.notify(f"Auto-optimization OFF for {self._team.nickname} "
                            "-- left solely to your manual click-to-swap.")

    # ------------------------------------------------------------ info panel

    def on_player_row_name_clicked(self, message: PlayerRow.NameClicked) -> None:
        self.app.push_screen(
            PlayerInfoScreen(message.player, self._team.provider, self._reg))

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
            # min_gain=-inf: min_gain_to_write exists to stop the *optimizer*
            # suggesting churn for negligible gain. A manual swap was already
            # approved by the user clicking Confirm on this exact pair, so it
            # shouldn't be second-guessed by that threshold too -- commit()
            # still keeps its is_noop and re-checked-locked-player guards.
            commit(prov, t, _swap_planner(pid_a, pid_b), self._week,
                   log_path=LOG_PATH, min_gain=float("-inf"))
        except (WriteRefused, VerificationFailed, NotSupported, ProviderError) as e:
            self.app.call_from_thread(self.app.notify, str(e), severity="error")
            return
        except Exception as e:  # pragma: no cover - unexpected provider failure
            self.app.call_from_thread(
                self.app.notify, f"Swap failed: {e}", severity="error")
            return
        self.app.call_from_thread(self.app.notify, "Lineup updated.")
        self.app.call_from_thread(self.load_roster)


class MatchupPlayerRow(Horizontal):
    """One player inside a Matchup column: badge, name, status, scored/proj.

    Deliberately lighter than PlayerRow -- Team/Opponent/Game don't fit two
    columns side by side, and this view is read-only (no Move, no name-click
    info panel; nothing here writes anything).
    """

    def __init__(self, player: Player) -> None:
        self.player = player
        super().__init__(classes="matchup-player-row")

    def compose(self) -> ComposeResult:
        p = self.player
        badge_cls = POSITION_CSS_CLASS.get(p.position, "pos-def")
        yield Label(p.position, classes=f"badge {badge_cls}")
        yield Label(p.name, classes="matchup-player-name")
        yield Static(avail_style(p), classes="matchup-player-avail")
        if p.actual_points is not None:
            yield Label(f"{p.actual_points:.1f}", classes="matchup-player-pts")
        else:
            proj = f"{p.projection:.1f}" if p.projection is not None else "-"
            yield Label(proj, classes="matchup-player-pts matchup-player-pts-proj")


class MatchupView(VerticalScroll):
    """One league's weekly scoreboard: week cycler, matchup picker, two rosters."""

    MIN_WEEK = 1
    MAX_WEEK = 18

    def __init__(self, team: TeamRef, reg: ProviderRegistry) -> None:
        self._team = team
        self._reg = reg
        self._week = 1
        self._matchups: list[MatchupSummary] = []
        super().__init__()

    def compose(self) -> ComposeResult:
        with Horizontal(id="week-cycler"):
            yield Button("<", id="week-prev")
            yield Label("Week", id="week-label")
            yield Button(">", id="week-next")
        yield Select([], id="matchup-select", prompt="Loading matchups...",
                    allow_blank=True)
        with Horizontal(id="matchup-columns"):
            yield Vertical(id="home-column", classes="matchup-column")
            yield Vertical(id="away-column", classes="matchup-column")

    def on_mount(self) -> None:
        prov, _ = self._reg.try_get(self._team.provider)
        if prov is not None:
            try:
                self._week = prov.current_week()
            except Exception:
                pass
        self.load_matchups()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "week-prev":
            self._week = max(self.MIN_WEEK, self._week - 1)
            self.load_matchups()
        elif event.button.id == "week-next":
            self._week = min(self.MAX_WEEK, self._week + 1)
            self.load_matchups()

    @work(exclusive=True, thread=True)
    def load_matchups(self) -> None:
        t = self._team
        try:
            prov, err = self._reg.try_get(t.provider)
            if prov is None or not hasattr(prov, "get_matchups"):
                raise RuntimeError(err or "Matchups aren't available for this platform.")
            matchups = prov.get_matchups(t.league_id, self._week)
        except Exception as e:
            self.app.call_from_thread(self._show_error, str(e))
            return
        self.app.call_from_thread(self._render_matchup_list, matchups)

    def _show_error(self, msg: str) -> None:
        self.query_one("#week-label", Label).update(f"Week {self._week}")
        select = self.query_one("#matchup-select", Select)
        select.set_options([])
        select.prompt = msg

    def _render_matchup_list(self, matchups: list[MatchupSummary]) -> None:
        self._matchups = matchups
        self.query_one("#week-label", Label).update(f"Week {self._week}")

        options = []
        default_value = None
        for i, m in enumerate(matchups):
            label = (f"{m.home_name} ({m.home_score:.1f}) vs "
                    f"{m.away_name} ({m.away_score:.1f})")
            options.append((label, i))
            if self._team.team_id in (m.home_team_id, m.away_team_id):
                default_value = i

        select = self.query_one("#matchup-select", Select)
        select.set_options(options)
        if not options:
            select.prompt = "No matchups found for this week."
            return
        select.value = default_value if default_value is not None else 0

    def on_select_changed(self, message: Select.Changed) -> None:
        if message.select.id != "matchup-select":
            return
        idx = message.value
        if not isinstance(idx, int) or idx >= len(self._matchups):
            return
        self.load_matchup_rosters(self._matchups[idx])

    @work(exclusive=True, thread=True)
    def load_matchup_rosters(self, m: MatchupSummary) -> None:
        t = self._team
        prov, err = self._reg.try_get(t.provider)
        if prov is None:
            self.app.call_from_thread(
                self.app.notify, err or "Provider unavailable", severity="error")
            return
        try:
            home_ref = TeamRef(provider=t.provider, league_id=t.league_id,
                               team_id=m.home_team_id, nickname=m.home_name)
            away_ref = TeamRef(provider=t.provider, league_id=t.league_id,
                               team_id=m.away_team_id, nickname=m.away_name)
            home_roster = prov.get_roster(home_ref, self._week)
            enrich(self._reg, t.provider, home_roster, self._week)
            away_roster = prov.get_roster(away_ref, self._week)
            enrich(self._reg, t.provider, away_roster, self._week)
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Couldn't load matchup rosters: {e}", severity="error")
            return
        self.app.call_from_thread(self._render_matchup_rosters, m, home_roster, away_roster)

    def _render_matchup_rosters(self, m: MatchupSummary, home_roster: Roster,
                                away_roster: Roster) -> None:
        home_col = self.query_one("#home-column", Vertical)
        home_col.remove_children()
        home_col.mount(Label(f"[bold]{m.home_name}[/bold]  "
                             f"{home_roster.projected_total:.1f}",
                             classes="matchup-team-label"))
        for p in sorted(home_roster.starters, key=display_key):
            home_col.mount(MatchupPlayerRow(p))

        away_col = self.query_one("#away-column", Vertical)
        away_col.remove_children()
        away_col.mount(Label(f"[bold]{m.away_name}[/bold]  "
                             f"{away_roster.projected_total:.1f}",
                             classes="matchup-team-label"))
        for p in sorted(away_roster.starters, key=display_key):
            away_col.mount(MatchupPlayerRow(p))


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
                        with TabbedContent(id=f"team-{i}-tabs"):
                            with TabPane("Roster", id=f"team-{i}-roster"):
                                yield RosterView(t, self.reg)
                            prov, _ = self.reg.try_get(t.provider)
                            if prov is not None and hasattr(prov, "get_matchups"):
                                with TabPane("Matchup", id=f"team-{i}-matchup"):
                                    yield MatchupView(t, self.reg)
        yield Footer()

    def action_refresh_active(self) -> None:
        tabs = self.query(TabbedContent)
        if not tabs:
            return
        outer_pane = tabs.first().active_pane
        if outer_pane is None:
            return
        inner_tabs = outer_pane.query(TabbedContent)
        inner_pane = inner_tabs.first().active_pane if inner_tabs else None
        target = inner_pane if inner_pane is not None else outer_pane

        rosters = target.query(RosterView)
        if rosters:
            rosters.first().load_roster()
            return
        matchups = target.query(MatchupView)
        if matchups:
            matchups.first().load_matchups()
