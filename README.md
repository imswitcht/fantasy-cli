# ff — four teams, three platforms, one terminal

A personal CLI for managing fantasy football teams across Yahoo, ESPN and
Sleeper. Reads every roster into one view, optimizes lineups against a shared
projection source, and writes changes back where the platform allows it.

## What actually works, honestly

| Platform | Read | Write lineups | Status |
|---|---|---|---|
| **Yahoo** | yes | **yes** | Official OAuth 2.0 API with documented write support. Safe to automate. |
| **ESPN** | yes | yes, unofficially | No public API. Uses your own session cookies against the endpoint the ESPN website uses. Works today; can break whenever ESPN ships. |
| **Sleeper** | yes | **no** | Sleeper's API is read-only by design. There is no supported write path, and this tool does not pretend otherwise. |

For Sleeper, `ff sub-plan` computes the starter/backup pairings you should put
into **Sleeper's own AutoSubs feature**, which does the inactive-player swap for
you natively. That's a better answer than a reverse-engineered write that
breaks every few weeks.

Worth knowing before you rely on any of this: **Yahoo and Sleeper both already
have built-in inactive-player protection** (Yahoo's "Start Active Players",
Sleeper's AutoSubs). ESPN does not. So the autopilot's real value is on the
ESPN team — which is also the one with the least stable write path. That irony
is the honest shape of the problem.

Where this tool clearly beats all three platforms' built-in features: it
optimizes on *projections*, not just on who is injured, and it shows you all
four teams side by side.

## Setup

```powershell
# 1. Create a virtualenv and install
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

# 2. Configure
copy config.example.toml config.toml
copy .env.example .env

# 3. Credentials — each command prints step-by-step instructions
python ff.py auth sleeper     # 10 seconds, just a username
python ff.py auth espn        # 2 minutes, browser cookies
python ff.py auth yahoo       # 5 minutes, one-time OAuth app

# 4. Find your league/team ids and paste them into config.toml
python ff.py teams --discover

# 5. Confirm everything works
python ff.py doctor
```

`rich` is optional. Without it the tables render as plain text.

## Daily use

```powershell
python ff.py status                  # all four rosters, one screen
python ff.py status --bench          # include benches
python ff.py optimize                # full start/sit suggestions (dry run)
python ff.py optimize --apply        # actually submit them
python ff.py optimize --team work    # just one team
python ff.py autopilot               # only swap out OUT/IR/bye starters
python ff.py sub-plan                # Sleeper AutoSubs pairings
```

**Everything is a dry run unless you pass `--apply`.** That is deliberate and
you should keep it that way for the first couple of weeks.

## Scheduling

```powershell
# Dry run first — writes to logs\ so you can see what it would have done
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1

# Once you trust it
powershell -ExecutionPolicy Bypass -File scripts\register-task.ps1 -Apply
```

Registers five weekly tasks covering Thursday, Sunday early/late/night, and
Monday night kickoffs. Adjust the times in `register-task.ps1` for your zone —
the defaults assume US Mountain time.

## How writes are kept safe

Every `--apply` follows four steps, and the fourth is the one most tools skip:

1. Re-read the roster immediately before writing — state moves fast on Sundays.
2. Recompute the plan against that fresh state; abort if it changed.
3. Submit.
4. **Re-read and verify the roster actually matches what was requested.**

Step 4 exists because ESPN will return `200 OK` on a write it did not apply. A
tool that trusts the status code tells you your lineup is fixed when it isn't,
which is worse than doing nothing. Every applied change is appended to
`changes.log` with the full before/after roster state.

Locked players — anyone whose game has kicked off — are pinned in place and
never included in a plan. This is enforced locally rather than trusted to the
platform to reject.

## When ESPN breaks

It will, eventually. Two failure modes:

- **401 on reads** — your cookies expired. Re-run `ff auth espn` and paste fresh
  values. Expect this roughly monthly.
- **Write rejected or unverified** — ESPN changed the request shape. Run
  `ff capture espn`, which walks you through copying the real request out of
  browser devtools; the driver will use the captured shape as its template.

## Layout

```
ff/
  models.py         canonical Player / Roster / Slot / LineupPlan
  providers/
    base.py         Provider interface + capability flags
    yahoo.py        official API, read + write
    espn.py         cookie auth, read + unofficial write
    sleeper.py      public read API, no write, projection source
  crosswalk.py      ESPN/Yahoo player ids -> Sleeper ids (for shared projections)
  optimizer.py      slot eligibility, lock rules, two planners
  writer.py         the read-recompute-submit-verify path
  cli.py            commands
  ui.py             rich, with a plain-text fallback
tests/              27 tests covering optimizer, locks and crosswalk
scripts/            PowerShell autopilot + Task Scheduler registration
```

## Tests

```powershell
python -m unittest discover -s tests -v
```

These cover the logic that costs you a week if it's wrong: slot eligibility,
game locks, inactive detection, and player ID matching. The provider drivers
can only be verified against live accounts — that's what `ff doctor` does.

## Security

`.env` holds your ESPN session cookies, which are as good as your ESPN
password. `secrets/` holds your Yahoo refresh token. Both are gitignored, both
stay on this machine, and nothing here ever transmits them anywhere except to
the platform they belong to.
