"""Command line interface."""
from __future__ import annotations

import sys
from typing import Optional

import click

from .ui import Panel, Table, Text, console
from .config import LOG_PATH, Config, ProviderRegistry, load_auto_optimize_overrides
from .display import WRITE_BADGE, avail_style, display_key, enrich
from .models import LineupPlan, Roster, TeamRef
from .optimizer import plan_inactive_swaps, plan_optimal, sub_pairings
from .providers.base import NotSupported, Provider, ProviderError
from .writer import VerificationFailed, WriteRefused, commit


def _fail(msg: str, code: int = 1):
    console.print(f"[bold red]Error:[/bold red] {msg}")
    sys.exit(code)


def _load() -> tuple[Config, ProviderRegistry]:
    try:
        cfg = Config.load()
    except Exception as e:
        _fail(str(e))
    return cfg, ProviderRegistry(cfg)


# --------------------------------------------------------------------- group

@click.group()
@click.version_option("0.1.0", prog_name="ff")
def cli():
    """Manage your fantasy football teams across Yahoo, ESPN and Sleeper."""


# -------------------------------------------------------------------- doctor

@cli.command()
def doctor():
    """Check config, credentials and each platform's capabilities."""
    cfg, reg = _load()
    console.print(Panel.fit(f"Season [bold]{cfg.season}[/bold] · "
                            f"{len(cfg.teams)} team(s) configured"))

    providers = sorted({t.provider for t in cfg.teams})
    table = Table(show_header=True, header_style="bold")
    table.add_column("Platform")
    table.add_column("Auth")
    table.add_column("Read")
    table.add_column("Write")
    table.add_column("Notes", overflow="fold")

    ok = True
    for name in providers:
        prov, err = reg.try_get(name)
        if prov is None:
            ok = False
            table.add_row(name, "[red]failed[/red]", "-", "-", err or "")
            continue
        try:
            identity = prov.check_auth()
            auth_cell = "[green]ok[/green]"
        except Exception as e:
            ok = False
            identity = str(e)
            auth_cell = "[red]failed[/red]"
        caps = prov.capabilities
        badge, _ = WRITE_BADGE.get(caps.write_status, ("?", "?"))
        table.add_row(
            name, auth_cell,
            "[green]yes[/green]" if caps.can_read else "[red]no[/red]",
            badge,
            f"{identity}\n[dim]{caps.write_caveat or ''}[/dim]",
        )
    console.print(table)

    console.print()
    for t in cfg.teams:
        prov, err = reg.try_get(t.provider)
        if prov is None:
            console.print(f"  [red]✘[/red] {t.nickname} ({t.provider}) - {err}")
            continue
        try:
            r = prov.get_roster(t)
            console.print(f"  [green]✔[/green] {t.nickname} ({t.provider}) - "
                          f"{len(r.players)} players, {len(r.starters)} starters")
        except Exception as e:
            ok = False
            console.print(f"  [red]✘[/red] {t.nickname} ({t.provider}) - {e}")

    sys.exit(0 if ok else 1)


# --------------------------------------------------------------------- teams

@cli.command()
@click.option("--discover", is_flag=True,
              help="Ask each platform what teams you own (Yahoo and Sleeper only).")
def teams(discover: bool):
    """List your configured teams, or discover them."""
    cfg, reg = _load()
    if not discover:
        table = Table()
        table.add_column("Nickname")
        table.add_column("Platform")
        table.add_column("League")
        table.add_column("Team")
        for t in cfg.teams:
            table.add_row(t.nickname, t.provider, t.league_id, t.team_id)
        console.print(table)
        return

    for name in ("yahoo", "sleeper", "espn"):
        prov, err = reg.try_get(name)
        if prov is None:
            console.print(f"[dim]{name}: {err}[/dim]")
            continue
        try:
            found = prov.discover_teams()
        except Exception as e:
            console.print(f"[dim]{name}: {e}[/dim]")
            continue
        for t in found:
            console.print(f"[green]{name}[/green]  league_id = \"{t.league_id}\"  "
                          f"team_id = \"{t.team_id}\"  # {t.nickname}")


# -------------------------------------------------------------------- status

@cli.command()
@click.option("--week", type=int, default=None)
@click.option("--team", "team_filter", default=None, help="Nickname substring.")
@click.option("--bench/--no-bench", default=False, help="Also show the bench.")
def status(week: Optional[int], team_filter: Optional[str], bench: bool):
    """Show every roster in one place."""
    cfg, reg = _load()
    for t in cfg.teams:
        if team_filter and team_filter.lower() not in t.nickname.lower():
            continue
        prov, err = reg.try_get(t.provider)
        if prov is None:
            console.print(f"[red]{t.nickname}: {err}[/red]\n")
            continue
        try:
            wk = week or prov.current_week()
            roster = prov.get_roster(t, wk)
            enrich(reg, t.provider, roster, wk)
        except Exception as e:
            console.print(f"[red]{t.nickname}: {e}[/red]\n")
            continue

        header = (f"[bold]{t.nickname}[/bold]  "
                  f"[dim]{t.provider} · {t.league_name or t.league_id} · "
                  f"week {wk}[/dim]")
        table = Table(title=header, title_justify="left", show_header=True,
                      header_style="bold")
        table.add_column("Slot", width=10)
        table.add_column("Player", overflow="fold")
        table.add_column("Pos", width=4)
        table.add_column("Team", width=5)
        table.add_column("Proj", justify="right", width=6)
        table.add_column("Status", width=13)

        rows = sorted(roster.starters, key=display_key)
        if bench:
            rows += sorted(roster.bench, key=display_key)
            rows += sorted(roster.injured_reserve, key=display_key)
        for p in rows:
            proj = f"{p.projection:.1f}" if p.projection is not None else "-"
            slot_txt = p.slot.value
            style = "dim" if not p.slot.is_starting else ""
            lock = " 🔒" if p.is_locked else ""
            table.add_row(
                Text(slot_txt, style=style),
                Text(p.name + lock, style=style),
                p.position, p.nfl_team or "-", proj, avail_style(p),
            )
        console.print(table)
        console.print(f"  Projected starters total: "
                      f"[bold]{roster.projected_total:.1f}[/bold]\n")


# ------------------------------------------------------------------ optimize

def _render_plan(plan: LineupPlan, provider: Provider) -> None:
    caps = provider.capabilities
    badge, _ = WRITE_BADGE.get(caps.write_status, ("?", "?"))
    console.print(f"[bold]{plan.team.nickname}[/bold] "
                  f"[dim]({plan.team.provider} · write: {badge})[/dim]")

    if plan.is_noop:
        console.print("  [green]Lineup is already optimal.[/green]")
    else:
        for m in plan.moves:
            arrow = "[green]→[/green]"
            proj = f"{m.player.projection:.1f}" if m.player.projection is not None else "?"
            flag = ""
            if m.player.availability.is_dead_weight:
                flag = f" [red]({m.player.availability.value})[/red]"
            console.print(f"  {m.player.name:<24} {m.from_slot.value:>7} "
                          f"{arrow} {m.to_slot.value:<7} [dim]{proj} pts[/dim]{flag}")
        console.print(f"  Projected: {plan.projected_before:.1f} → "
                      f"[bold]{plan.projected_after:.1f}[/bold] "
                      f"({plan.gain:+.1f})")

    for note in plan.blocked:
        console.print(f"  [yellow]·[/yellow] [dim]{note}[/dim]")
    console.print()


def _run_planner(planner, week, team_filter, apply_it, mode_label):
    cfg, reg = _load()
    overrides = load_auto_optimize_overrides()
    any_written = False
    for t in cfg.teams:
        if team_filter and team_filter.lower() not in t.nickname.lower():
            continue
        prov, err = reg.try_get(t.provider)
        if prov is None:
            console.print(f"[red]{t.nickname}: {err}[/red]\n")
            continue
        try:
            wk = week or prov.current_week()
            roster = prov.get_roster(t, wk)
            enrich(reg, t.provider, roster, wk)
            plan = planner(t, roster)
        except Exception as e:
            console.print(f"[red]{t.nickname}: {e}[/red]\n")
            continue

        _render_plan(plan, prov)

        if not apply_it or plan.is_noop:
            continue

        if not overrides.get(t.key, True):
            console.print(f"  [dim]Auto-optimization is off for {t.nickname} -- "
                          "left to your discretion.[/dim]\n")
            continue

        caps = prov.capabilities
        if not caps.can_write_lineup:
            console.print(f"  [yellow]Cannot write to {t.provider}.[/yellow] "
                          f"{caps.write_caveat}")
            console.print(f"  Open: {prov.team_url(t)}\n")
            continue

        if caps.write_status != "official":
            console.print(f"  [yellow]Heads up:[/yellow] {caps.write_caveat}")

        try:
            def _replan(team, fresh):
                enrich(reg, team.provider, fresh, wk)
                return planner(team, fresh)

            done = commit(prov, t, _replan, wk,
                          log_path=LOG_PATH, min_gain=cfg.min_gain_to_write)
            console.print(f"  [green]Applied and verified[/green] "
                          f"({len(done.moves)} move(s)).\n")
            any_written = True
        except WriteRefused as e:
            console.print(f"  [dim]Skipped: {e}[/dim]\n")
        except VerificationFailed as e:
            console.print(f"  [bold red]{e}[/bold red]\n")
        except NotSupported as e:
            console.print(f"  [yellow]{e}[/yellow]\n")
        except ProviderError as e:
            console.print(f"  [red]Write failed: {e}[/red]\n")

    if apply_it and any_written:
        console.print(f"[dim]Change log: {LOG_PATH}[/dim]")


@cli.command()
@click.option("--week", type=int, default=None)
@click.option("--team", "team_filter", default=None, help="Nickname substring.")
@click.option("--apply", "apply_it", is_flag=True,
              help="Actually submit the changes. Without this it's a dry run.")
def optimize(week, team_filter, apply_it):
    """Full projection-based start/sit optimization (dry run by default)."""
    _run_planner(plan_optimal, week, team_filter, apply_it, "optimize")


@cli.command()
@click.option("--week", type=int, default=None)
@click.option("--team", "team_filter", default=None)
@click.option("--apply", "apply_it", is_flag=True)
def autopilot(week, team_filter, apply_it):
    """Replace only starters who are OUT, IR, suspended or on bye.

    This is the conservative mode meant for the scheduled task.
    """
    _run_planner(plan_inactive_swaps, week, team_filter, apply_it, "autopilot")


# ------------------------------------------------------------------ sub-plan

@cli.command(name="sub-plan")
@click.option("--week", type=int, default=None)
def sub_plan(week):
    """Print starter/backup pairings to configure in Sleeper's AutoSubs.

    Sleeper won't let anything write its lineups, but it will do the swap
    itself if you tell it the pairings. This computes them for you.
    """
    cfg, reg = _load()
    sleeper_teams = [t for t in cfg.teams if t.provider == "sleeper"]
    if not sleeper_teams:
        console.print("No Sleeper teams configured.")
        return
    prov, err = reg.try_get("sleeper")
    if prov is None:
        _fail(err or "Sleeper unavailable")

    for t in sleeper_teams:
        wk = week or prov.current_week()
        roster = prov.get_roster(t, wk)
        enrich(reg, "sleeper", roster, wk)
        console.print(f"[bold]{t.nickname}[/bold] [dim]· set these in "
                      f"Sleeper → team → AutoSubs[/dim]")
        pairs = sub_pairings(roster, t.slot_layout)
        if not pairs:
            console.print("  [dim]No eligible backups on the bench.[/dim]")
        for starter, backup in pairs:
            bp = f"{backup.projection:.1f}" if backup.projection is not None else "?"
            console.print(f"  {starter.name:<24} [dim]sub →[/dim] "
                          f"{backup.name:<24} [dim]{bp} pts[/dim]")
        console.print()


# ---------------------------------------------------------------------- auth

AUTH_HELP = {
    "yahoo": """[bold]Yahoo setup[/bold] (one time, ~5 minutes)

1. Go to https://developer.yahoo.com/apps/create/
2. Application Type: [bold]Installed Application[/bold]
   Redirect URI: [bold]oob[/bold]
   API Permissions: tick [bold]Fantasy Sports[/bold] → [bold]Read/Write[/bold]
   (Read/Write matters -- Read-only cannot set lineups.)
3. Create the app and copy the Client ID and Client Secret.
4. Create [bold]secrets/yahoo_token.json[/bold] containing:

   {"consumer_key": "YOUR_CLIENT_ID", "consumer_secret": "YOUR_CLIENT_SECRET"}

5. Run [bold]ff doctor[/bold]. It will open a Yahoo consent page and ask you to
   paste back the verification code. After that the file holds a refresh token
   and you won't be asked again.""",

    "espn": """[bold]ESPN setup[/bold] (one time, ~2 minutes, repeat ~monthly)

ESPN has no API keys, so this uses your own session cookies.

1. Sign in at https://fantasy.espn.com in Chrome or Edge.
2. Press F12 → [bold]Application[/bold] tab → Storage → Cookies → https://fantasy.espn.com
3. Copy the values of [bold]espn_s2[/bold] and [bold]SWID[/bold].
   SWID includes the curly braces -- keep them.
4. Put them in [bold].env[/bold]:

   ESPN_S2=AEB...long...string
   ESPN_SWID={XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}

5. Your league id and team id are in your team URL:
   fantasy.espn.com/football/team?leagueId=[bold]123456[/bold]&teamId=[bold]3[/bold]

[yellow]These cookies are as good as your ESPN password. They live only in .env
on this machine, which is gitignored. They expire every few weeks -- when
`ff doctor` reports a 401, repeat steps 2-4.[/yellow]""",

    "sleeper": """[bold]Sleeper setup[/bold] (one time, ~10 seconds)

Sleeper's read API needs no authentication at all. Just your username:

   SLEEPER_USERNAME=your_sleeper_username

Then run [bold]ff teams --discover[/bold] to get your league and roster ids.

[yellow]Sleeper cannot be written to -- its API is read-only by design. Use
`ff sub-plan` and configure Sleeper's built-in AutoSubs instead.[/yellow]""",
}


@cli.command()
@click.argument("platform", type=click.Choice(["yahoo", "espn", "sleeper"]))
def auth(platform: str):
    """Show credential setup instructions for a platform."""
    console.print(Panel(AUTH_HELP[platform], expand=False))


@cli.command()
@click.argument("platform", type=click.Choice(["espn", "sleeper"]))
def capture(platform: str):
    """Instructions for recording a platform's real lineup request.

    Use this when a write starts failing because the platform changed its API.
    """
    console.print(Panel(f"""[bold]Capturing the real {platform} lineup request[/bold]

1. Open your {platform} team in Chrome and press F12 → [bold]Network[/bold] tab.
2. Tick [bold]Preserve log[/bold] and filter to [bold]Fetch/XHR[/bold].
3. Make any harmless lineup change in the UI (swap two bench players).
4. Find the request that fired -- for ESPN it posts to a URL containing
   [bold]/transactions/[/bold]; for Sleeper it will be a GraphQL POST.
5. Right-click it → Copy → [bold]Copy request payload[/bold].
6. Save it as [bold]secrets/{platform}_capture.json[/bold].

The driver will use the captured shape as its template, substituting your own
team id, week and player moves. If the platform also changed its auth headers,
copy those in as a "headers" key alongside the payload.""", expand=False))


# ------------------------------------------------------------------------ tui

@cli.command()
def tui():
    """Launch the interactive Sleeper-style roster browser."""
    try:
        from .tui import FantasyTUI
    except ImportError:
        _fail("textual is not installed. Run: pip install textual")
        return
    FantasyTUI().run()


def main():
    try:
        cli()
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
