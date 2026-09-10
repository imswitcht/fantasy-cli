"""Cross-platform player identity.

Sleeper's own player catalog carries `espn_id` and `yahoo_id` on most players,
which makes this far less painful than it usually is -- no external ID map to
keep in sync. Where those are missing (rookies, practice-squad callups, defenses)
we fall back to a normalized name + position match.

The point of all this is to attach one projection source to all four teams, so
"start Player A over Player B" means the same thing on every platform.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

from .models import Player

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def normalize_name(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = re.sub(r"[^a-z\s]", "", name)
    parts = [p for p in name.split() if p not in SUFFIXES]
    return " ".join(parts)


@dataclass
class Crosswalk:
    """Maps a platform player id to a Sleeper player id."""

    by_espn: dict[str, str] = field(default_factory=dict)
    by_yahoo: dict[str, str] = field(default_factory=dict)
    by_name_pos: dict[tuple[str, str], str] = field(default_factory=dict)

    @classmethod
    def from_sleeper_catalog(cls, catalog: dict) -> "Crosswalk":
        cw = cls()
        for sleeper_id, meta in catalog.items():
            espn_id = meta.get("espn_id")
            yahoo_id = meta.get("yahoo_id")
            if espn_id:
                cw.by_espn[str(espn_id)] = sleeper_id
            if yahoo_id:
                cw.by_yahoo[str(yahoo_id)] = sleeper_id

            pos = (meta.get("position") or "").upper().replace("DST", "DEF")
            full = meta.get("full_name") or " ".join(
                filter(None, [meta.get("first_name"), meta.get("last_name")]))
            if full and pos:
                cw.by_name_pos.setdefault((normalize_name(full), pos), sleeper_id)
            # Team defenses are keyed by team abbreviation in Sleeper.
            if pos == "DEF" and meta.get("team"):
                cw.by_name_pos.setdefault(
                    (normalize_name(meta["team"]), "DEF"), sleeper_id)
        return cw

    def resolve(self, player: Player, provider: str) -> Optional[str]:
        if provider == "sleeper":
            return player.platform_id
        table = self.by_espn if provider == "espn" else self.by_yahoo
        hit = table.get(str(player.platform_id))
        if hit:
            return hit
        key = (normalize_name(player.name), player.position.upper())
        hit = self.by_name_pos.get(key)
        if hit:
            return hit
        # Defenses are named inconsistently across platforms ("Bears D/ST",
        # "Chicago", "CHI"). Try the team abbreviation.
        if player.position.upper() == "DEF" and player.nfl_team:
            return self.by_name_pos.get((normalize_name(player.nfl_team), "DEF"))
        return None


def attach_projections(players: list[Player], provider: str,
                       crosswalk: Crosswalk,
                       projections: dict[str, float],
                       overwrite: bool = False) -> tuple[int, list[str]]:
    """Fill in .projection from the shared source. Returns (matched, unmatched names)."""
    matched = 0
    misses: list[str] = []
    for p in players:
        if p.projection is not None and not overwrite:
            matched += 1
            continue
        sid = crosswalk.resolve(p, provider)
        p.canonical_id = sid
        if sid and sid in projections:
            p.projection = projections[sid]
            matched += 1
        else:
            misses.append(p.name)
    return matched, misses
