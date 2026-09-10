"""Diagnostic: show what the tool thinks the current week and kickoff times are.

Run:  python scripts\check_locks.py

A player is treated as locked when their game's kickoff is in the past. If the
kickoff map is wrong -- wrong week, wrong season, empty response -- players get
falsely locked and the optimizer silently refuses to move them. This prints the
raw inputs so you can see which part is off.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from ff.config import Config, ProviderRegistry

cfg = Config.load()
reg = ProviderRegistry(cfg)

now = datetime.now(timezone.utc)
print(f"Now (UTC):   {now:%Y-%m-%d %H:%M} ")
print(f"Now (local): {datetime.now():%Y-%m-%d %H:%M %Z}")
print(f"Season configured: {cfg.season}\n")

# What each provider thinks the week is.
for name in sorted({t.provider for t in cfg.teams}):
    prov, err = reg.try_get(name)
    if prov is None:
        print(f"{name:8} week: ERROR {err}")
        continue
    try:
        print(f"{name:8} week: {prov.current_week()}")
    except Exception as e:
        print(f"{name:8} week: ERROR {e}")

# Sleeper's state endpoint is the authoritative answer.
try:
    state = requests.get("https://api.sleeper.app/v1/state/nfl", timeout=20).json()
    print(f"\nSleeper state/nfl (authoritative):")
    for k in ("season", "week", "display_week", "season_type", "leg"):
        if k in state:
            print(f"  {k:14} = {state[k]}")
except Exception as e:
    print(f"\nSleeper state fetch failed: {e}")

# The kickoff map the lock logic actually uses.
week = None
for name in ("sleeper", "espn"):
    prov, _ = reg.try_get(name)
    if prov:
        try:
            week = prov.current_week()
            break
        except Exception:
            pass

print(f"\nESPN scoreboard for week={week}, seasontype=2, dates={cfg.season}:")
try:
    r = requests.get(
        "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
        params={"week": week, "seasontype": 2, "dates": cfg.season}, timeout=20)
    r.raise_for_status()
    events = r.json().get("events", [])
    if not events:
        print("  !! NO GAMES RETURNED -- kickoff map is empty.")
        print("     Nothing would be treated as locked. (Fails open, not shut.)")
    for game in sorted(events, key=lambda g: g["date"]):
        start = datetime.fromisoformat(game["date"].replace("Z", "+00:00"))
        teams = "/".join(
            c.get("team", {}).get("abbreviation", "?")
            for c in game.get("competitions", [{}])[0].get("competitors", []))
        state_txt = game.get("status", {}).get("type", {}).get("name", "?")
        flag = "LOCKED " if now >= start else "       "
        print(f"  {flag} {teams:12} {start:%a %m-%d %H:%M} UTC  "
              f"{start.astimezone():%a %m-%d %I:%M%p} local   [{state_txt}]")
except Exception as e:
    print(f"  scoreboard fetch failed: {e}")

print("\nIf SEA shows LOCKED but their game hasn't actually started, the week "
      "number or the season/dates parameter is wrong.")
