# CLAUDE.md — context for working on this project

A personal CLI that reads four fantasy football teams (2 Yahoo, 1 ESPN,
1 Sleeper) into one view and sets lineups where the platform allows it.
Single user, runs locally on Windows. Not a product.

## Platform reality — do not "fix" these by adding write support

| Platform | Read | Write | Notes |
|---|---|---|---|
| ESPN | works | unofficial | No public API. Session cookies (`espn_s2`, `SWID`) against the endpoint the ESPN site uses. Cookies expire ~monthly → 401. |
| Sleeper | works | **impossible** | Sleeper's API is documented read-only: "you cannot modify contents via this API." Do not add a write path. Use `ff sub-plan` + Sleeper's native AutoSubs instead. |
| Yahoo | **blocked** | **blocked** | As of the 2026 season Yahoo removed Fantasy Sports from self-serve app creation. Access requires an application at https://sports.yahoo.com/developer/access/ and Yahoo states write access "is not available at this time". Application submitted 2026-09-10, pending. The driver is written and targets official endpoints; it simply can't be provisioned yet. |

Yahoo ("Start Active Players") and Sleeper (AutoSubs) both have native
inactive-player protection. ESPN does not. So the autopilot's real value is on
ESPN — which is also the least stable write path. That's the honest shape of it.

## Hard-won gotchas

**ESPN has two colliding id spaces.** `defaultPositionId` is a POSITION id
(1=QB, 2=RB, 3=WR, 4=TE, 5=K, 16=DEF). `eligibleSlots` is a list of LINEUP SLOT
ids (0=QB, 2=RB, 3=RB/WR, 4=WR, 5=WR/TE, 6=TE, 16=D/ST, 17=K, 20=BE, 21=IR,
23=FLEX). Reading `eligibleSlots` through the position table made every WR
kicker-eligible (slot 5 = WR/TE collides with position 5 = K), and the optimizer
duly suggested starting a receiver at K. Only *single-position* slots may
contribute a position to a player.

**Locks are real and non-obvious.** The tool correctly flagged Seattle players
as locked mid-week because of an NFL game played in Australia. Do not "fix"
surprising locks without checking `scripts/check_locks.py` first.

**Verify writes, never trust status codes.** ESPN returns 200 on writes it did
not apply. `ff/writer.py` re-reads and confirms; keep that.

**Textual: never put a border on a `height: 1` widget.** The border eats the
widget's only line, leaving zero content height and rendering it blank. Hit
this twice in `ff/tui.tcss` — once on `.player-row` (rows went fully blank),
once on `TabbedContent Tab.-active` (the *selected* tab's label went blank,
so it looked like the other tab was selected). Fix in both cases: drop the
border, use a background-color change instead. If a row/tab/etc. needs a
border, give it `height: 2+` to leave room for both.

## Eligibility rules — the canonical table

`SLOT_ELIGIBILITY` in `ff/models.py` is the ONLY authority on lineup legality.

```
         QB   RB   WR   TE  FLEX WRRB WRTE SFLEX   K  DEF
QB      yes    .    .    .    .    .    .   yes    .    .
RB        .  yes    .    .  yes  yes    .   yes    .    .
WR        .    .  yes    .  yes  yes  yes   yes    .    .
TE        .    .    .  yes  yes    .  yes   yes    .    .
K         .    .    .    .    .    .    .     .  yes    .
DEF       .    .    .    .    .    .    .     .    .  yes
```

Enforced in three layers, all of which must stay:
1. `Player.__post_init__` sanitizes provider-supplied eligibility. K and DEF are
   exclusive; skill positions may only mix among QB/RB/WR/TE (this preserves
   real dual eligibility like RB/WR, and Taysom Hill at QB/TE).
2. `_build_plan` checks assignments before building moves.
3. `validate_plan` gates every plan and raises `IllegalLineup`.

## Safety rules

- **Dry run is the default.** Nothing writes without `--apply`. Keep it that way.
- `plan_inactive_swaps` (autopilot) only replaces OUT / IR / suspended / bye
  starters. It must stay conservative — it's what the scheduled task runs.
  `plan_optimal` is the opinionated one and is for human review.
- Locked players are never included in a plan.
- Never commit `.env`, `secrets/`, or `config.toml`. Check `git status` before
  any commit; a real bug once staged `config.toml` because .gitignore does not
  support trailing comments on a pattern line.
- `ff tui` (`ff/tui.py`) has one write path: each writable row has a small
  Move button; click two eligible players' Move buttons to swap them. It's
  gated on `provider.capabilities.can_write_lineup` (today, effectively ESPN
  only) and always shows a confirm dialog with the projected point change
  before calling `ff/writer.py`'s `commit()` — the same read → recompute →
  submit → verify path `--apply` uses. Don't add a way to skip that confirm
  step. Clicking a player's *name* (any platform) instead opens a read-only
  info panel — that's a separate, non-write interaction; don't conflate the
  two triggers.
- Manual TUI swaps always pass `min_gain=float("-inf")` to `commit()` — the
  confirm dialog is the approval, so `min_gain_to_write` never re-judges a
  swap the user already clicked Confirm on.
- Each writable team's page also has an "Auto-optimization" switch (default
  ON). OFF makes `ff optimize --apply` / `ff autopilot --apply` skip that team
  entirely, leaving it solely to the user's manual click-to-swap — it has no
  effect on manual swaps either way. Per-team state lives in
  `.cache/auto_optimize_overrides.json`, not `config.toml` (tomllib can't
  write TOML back without losing the user's comments) — see
  `ff/config.py:load_auto_optimize_overrides`. When ON, `min_gain_to_write`
  from `config.toml` still applies exactly as before; this switch is a
  separate on/off gate, not a floor adjustment.

## Testing

```powershell
python -m unittest discover -s tests
```

57 tests. Two things to know:

- The first 27 tests built players by hand with eligibility pre-set, so they
  tested the optimizer's reasoning and never exercised `_parse_player`. That
  gap is exactly how the WR-at-kicker bug survived. **When you touch a provider
  parser, add a test that feeds it a realistic raw payload.**
- `test_optimizer_never_emits_illegal_lineup_under_fuzz` runs 400 random
  rosters. It found a real bug in `plan_inactive_swaps` on its first run. Keep it.

## Layout

```
ff/models.py           Player/Roster/Slot/LineupPlan + eligibility table
ff/providers/base.py   Provider interface + capability flags
ff/providers/{espn,sleeper,yahoo}.py
ff/crosswalk.py        ESPN/Yahoo ids -> Sleeper ids via Sleeper's own catalog
ff/schedule.py         shared NFL schedule lookup (opponent, live game status)
ff/optimizer.py        two planners, lock handling, plan validation
ff/writer.py           read → recompute → submit → verify
ff/cli.py              commands
ff/ui.py               rich, with plain-text fallback (rich is optional)
ff/display.py          display helpers shared by ff/cli.py and ff/tui.py
ff/tui.py / ff/tui.tcss  `ff tui`, the interactive roster browser (optional, needs textual)
scripts/check_locks.py diagnostic for surprising lock behavior
```

## Style

Prefer honest failure over silent degradation. If a platform can't do
something, say so in `capabilities` and degrade to a deep link — never fake it.
Comments should explain *why*, especially where a platform's API is
counterintuitive.
