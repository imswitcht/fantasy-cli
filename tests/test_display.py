"""Tests for the presentation helpers shared by the CLI and the TUI.

These are pure functions extracted from ff/cli.py into ff/display.py so
ff/tui.py can reuse them without a circular import -- see CLAUDE.md and the
plan behind `ff tui`. Only the ordering and coloring logic is tested here;
enrich() hits the network via ProviderRegistry and isn't covered by this
offline suite (same boundary ff doctor already exists for).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ff.display import avail_style, display_key
from ff.models import Availability, Player, Slot


def mk(name, pos, slot, proj=10.0, avail=Availability.ACTIVE):
    return Player(
        platform_id=name.lower().replace(" ", ""),
        name=name, position=pos, slot=slot, projection=proj,
        availability=avail, eligible_positions={pos},
    )


class TestDisplayKey(unittest.TestCase):
    def test_slots_sort_in_sleeper_order(self):
        players = [
            mk("K1", "K", Slot.K),
            mk("QB1", "QB", Slot.QB),
            mk("DEF1", "DEF", Slot.DEF),
            mk("WR1", "WR", Slot.WR),
            mk("RB1", "RB", Slot.RB),
            mk("TE1", "TE", Slot.TE),
            mk("FLEX1", "RB", Slot.FLEX),
            mk("BENCH1", "WR", Slot.BENCH),
        ]
        ordered = sorted(players, key=display_key)
        self.assertEqual(
            [p.name for p in ordered],
            ["QB1", "RB1", "WR1", "TE1", "FLEX1", "K1", "DEF1", "BENCH1"],
        )

    def test_higher_projection_sorts_first_within_a_slot(self):
        low = mk("Low", "RB", Slot.BENCH, proj=4.0)
        high = mk("High", "RB", Slot.BENCH, proj=9.0)
        ordered = sorted([low, high], key=display_key)
        self.assertEqual([p.name for p in ordered], ["High", "Low"])


class TestAvailStyle(unittest.TestCase):
    def test_active_player_has_no_style(self):
        p = mk("Healthy", "WR", Slot.WR, avail=Availability.ACTIVE)
        self.assertEqual(avail_style(p).plain, "")

    def test_out_and_ir_and_bye_are_red(self):
        for status in (Availability.OUT, Availability.INJURED_RESERVE,
                       Availability.SUSPENDED, Availability.BYE,
                       Availability.DOUBTFUL):
            p = mk("Hurt", "RB", Slot.RB, avail=status)
            text = avail_style(p)
            self.assertEqual(text.plain, status.value)
            self.assertEqual(text.style, "red", msg=f"{status} should be red")

    def test_questionable_is_yellow(self):
        p = mk("Iffy", "TE", Slot.TE, avail=Availability.QUESTIONABLE)
        text = avail_style(p)
        self.assertEqual(text.style, "yellow")


if __name__ == "__main__":
    unittest.main()
