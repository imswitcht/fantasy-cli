"""Configuration and credential loading.

Secrets live in .env (gitignored). Team definitions live in config.toml, which
is safe to keep around -- league and team ids are not sensitive.
"""
from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import TeamRef
from .providers.base import Provider, ProviderError

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"
ENV_PATH = ROOT / ".env"
CACHE_DIR = ROOT / ".cache"
SECRETS_DIR = ROOT / "secrets"
LOG_PATH = ROOT / "changes.log"


def load_env(path: Path = ENV_PATH) -> None:
    """Minimal .env loader -- avoids a dependency for ten lines of parsing."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


@dataclass
class Config:
    season: int
    teams: list[TeamRef] = field(default_factory=list)
    # Behavior
    questionable_is_dead: bool = False
    min_gain_to_write: float = 0.0

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Config":
        load_env()
        if not path.exists():
            raise FileNotFoundError(
                f"No config at {path}. Copy config.example.toml to config.toml "
                "and fill in your four teams, then run `ff doctor`."
            )
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
        teams = []
        for t in raw.get("teams", []):
            missing = [k for k in ("provider", "league_id", "team_id") if k not in t]
            if missing:
                raise ValueError(
                    f"Team entry {t.get('nickname', '?')} is missing: {', '.join(missing)}")
            teams.append(TeamRef(
                provider=t["provider"].lower(),
                league_id=str(t["league_id"]),
                team_id=str(t["team_id"]),
                nickname=t.get("nickname", f"{t['provider']} team"),
            ))
        return cls(
            season=int(raw.get("season", 2026)),
            teams=teams,
            questionable_is_dead=bool(raw.get("questionable_is_dead", False)),
            min_gain_to_write=float(raw.get("min_gain_to_write", 0.0)),
        )


class ProviderRegistry:
    """Builds providers lazily, so one broken credential doesn't kill the run."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cache: dict[str, Provider] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.Lock()
        CACHE_DIR.mkdir(exist_ok=True)
        SECRETS_DIR.mkdir(exist_ok=True)

    def get(self, name: str) -> Provider:
        # The TUI loads every team's tab from its own background thread, so
        # two teams on the same platform (e.g. both Yahoo teams, or any team
        # plus the shared Sleeper projection source) can call this at once.
        # Without the lock, both threads see a cache miss and each builds
        # its own Provider -- duplicate sessions/logins racing to win the
        # cache slot.
        name = name.lower()
        if name in self._cache:
            return self._cache[name]
        with self._lock:
            if name in self._cache:
                return self._cache[name]
            if name in self._errors:
                raise ProviderError(self._errors[name])
            try:
                self._cache[name] = self._build(name)
            except Exception as e:
                self._errors[name] = str(e)
                raise
            return self._cache[name]

    def try_get(self, name: str) -> tuple[Optional[Provider], Optional[str]]:
        try:
            return self.get(name), None
        except Exception as e:
            return None, str(e)

    def _build(self, name: str) -> Provider:
        if name == "espn":
            from .providers.espn import EspnProvider
            return EspnProvider(
                espn_s2=os.environ.get("ESPN_S2", ""),
                swid=os.environ.get("ESPN_SWID", ""),
                season=self.cfg.season,
                capture_path=SECRETS_DIR / "espn_capture.json",
            )
        if name == "sleeper":
            from .providers.sleeper import SleeperProvider
            username = os.environ.get("SLEEPER_USERNAME", "")
            if not username:
                raise ProviderError("SLEEPER_USERNAME is not set in .env")
            return SleeperProvider(
                username=username,
                season=self.cfg.season,
                cache_dir=CACHE_DIR,
                capture_path=SECRETS_DIR / "sleeper_capture.json",
            )
        if name == "yahoo":
            from .providers.yahoo import YahooProvider
            token = Path(os.environ.get(
                "YAHOO_TOKEN_PATH", str(SECRETS_DIR / "yahoo_token.json")))
            return YahooProvider(token_path=token, season=self.cfg.season)
        raise ProviderError(f"Unknown provider '{name}'")

    @property
    def sleeper_for_data(self):
        """Sleeper doubles as the shared projection + crosswalk source."""
        p, err = self.try_get("sleeper")
        return p
