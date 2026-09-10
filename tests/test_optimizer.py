"""Tests for the parts that can be tested without hitting a platform.

The optimizer and lock logic are where a bug actually costs you a week, so they
get real coverage. The provider drivers can only be verified against live
accounts -- that's what `ff doctor` is for.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ff.crosswalk import Crosswalk, attach_projections, normalize_name
from ff.models import Availability, Player, Roster, Slot, TeamRef
from ff.optimizer import plan_inactive_swaps, plan_optimal, sub_pairings

PAST = datetime.now(timezone.utc) - timedelta(hours=2)
FUTURE = datetime.now(timezone.utc) + timedelta(hours=6)

STANDARD = [Slot.QB, Slot.RB, Slot.RB, Slot.WR, Slot.WR,
            Slot.TE, Slot.FLEX, Slot.K, Slot.DEF]


def mk(name, pos, slot, proj, avail=Availability.ACTIVE,
       kickoff=FUTURE, eligible=None, pid=None):
    return Player(
        platform_id=pid or name.lower().replace(" ", ""),
        name=name, position=pos, slot=slot, projection=proj,
        availability=avail, kickoff=kickoff,
        eligible_positions=set(eligible or [pos]), nfl_team="XX",
    )


def team():
    return TeamRef(provider="test", league_id="1", team_id="1",
                   nickname="Test", slot_layout=list(STANDARD))


class TestSlotEligibility(unittest.TestCase):
    def test_flex_accepts_rb_wr_te_only(self):
        self.assertTrue(mk("A", "RB", Slot.BENCH, 10).can_fill(Slot.FLEX))
        self.assertTrue(mk("B", "WR", Slot.BENCH, 10).can_fill(Slot.FLEX))
        self.assertTrue(mk("C", "TE", Slot.BENCH, 10).can_fill(Slot.FLEX))
        self.assertFalse(mk("D", "QB", Slot.BENCH, 10).can_fill(Slot.FLEX))
        self.assertFalse(mk("E", "K", Slot.BENCH, 10).can_fill(Slot.FLEX))

    def test_superflex_accepts_qb(self):
        self.assertTrue(mk("F", "QB", Slot.BENCH, 20).can_fill(Slot.SUPERFLEX))

    def test_multi_eligible_player(self):
        rb_wr = mk("G", "RB", Slot.BENCH, 12, eligible=["RB", "WR"])
        self.assertTrue(rb_wr.can_fill(Slot.WR))
        self.assertTrue(rb_wr.can_fill(Slot.RB))


class TestLocks(unittest.TestCase):
    def test_kickoff_in_past_is_locked(self):
        self.assertTrue(mk("H", "RB", Slot.RB, 10, kickoff=PAST).is_locked)

    def test_kickoff_in_future_is_not(self):
        self.assertFalse(mk("I", "RB", Slot.RB, 10, kickoff=FUTURE).is_locked)

    def test_unknown_kickoff_is_not_locked(self):
        self.assertFalse(mk("J", "RB", Slot.RB, 10, kickoff=None).is_locked)

    def test_locked_out_starter_is_never_moved(self):
        """A player whose game already started stays put even if he's OUT."""
        roster = Roster([
            mk("QB1", "QB", Slot.QB, 18),
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 0, Availability.OUT, kickoff=PAST),
            mk("WR1", "WR", Slot.WR, 13),
            mk("WR2", "WR", Slot.WR, 11),
            mk("TE1", "TE", Slot.TE, 8),
            mk("FLX", "WR", Slot.FLEX, 9),
            mk("K1", "K", Slot.K, 7),
            mk("DEF1", "DEF", Slot.DEF, 6),
            mk("RB3", "RB", Slot.BENCH, 12),
        ])
        plan = plan_inactive_swaps(team(), roster)
        moved = {m.player.name for m in plan.moves}
        self.assertNotIn("RB2", moved)
        self.assertTrue(any("locked" in b for b in plan.blocked))


class TestInactiveSwaps(unittest.TestCase):
    def base_roster(self, **overrides):
        players = [
            mk("QB1", "QB", Slot.QB, 18),
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 11),
            mk("WR1", "WR", Slot.WR, 13),
            mk("WR2", "WR", Slot.WR, 10),
            mk("TE1", "TE", Slot.TE, 8),
            mk("FLX", "WR", Slot.FLEX, 9),
            mk("K1", "K", Slot.K, 7),
            mk("DEF1", "DEF", Slot.DEF, 6),
            mk("RBbench", "RB", Slot.BENCH, 12),
            mk("WRbench", "WR", Slot.BENCH, 5),
        ]
        return Roster(players)

    def test_healthy_lineup_is_untouched(self):
        plan = plan_inactive_swaps(team(), self.base_roster())
        self.assertTrue(plan.is_noop, f"unexpected moves: {plan.moves}")

    def test_out_starter_is_replaced(self):
        roster = self.base_roster()
        rb2 = roster.by_platform_id("rb2")
        rb2.availability = Availability.OUT
        plan = plan_inactive_swaps(team(), roster)
        names = {m.player.name: m.to_slot for m in plan.moves}
        self.assertEqual(names.get("RBbench"), Slot.RB)
        self.assertEqual(names.get("RB2"), Slot.BENCH)

    def test_bye_week_starter_is_replaced(self):
        roster = self.base_roster()
        roster.by_platform_id("rb2").availability = Availability.BYE
        plan = plan_inactive_swaps(team(), roster)
        self.assertIn("RBbench", {m.player.name for m in plan.moves})

    def test_questionable_starter_is_left_alone(self):
        """Autopilot is the safety net, not the optimizer. Q is not a zero."""
        roster = self.base_roster()
        roster.by_platform_id("rb2").availability = Availability.QUESTIONABLE
        plan = plan_inactive_swaps(team(), roster)
        self.assertTrue(plan.is_noop)

    def test_no_eligible_replacement_is_reported_not_silent(self):
        roster = Roster([
            mk("QB1", "QB", Slot.QB, 0, Availability.OUT),
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 11),
            mk("WR1", "WR", Slot.WR, 13),
            mk("WR2", "WR", Slot.WR, 10),
            mk("TE1", "TE", Slot.TE, 8),
            mk("FLX", "WR", Slot.FLEX, 9),
            mk("K1", "K", Slot.K, 7),
            mk("DEF1", "DEF", Slot.DEF, 6),
        ])
        plan = plan_inactive_swaps(team(), roster)
        self.assertTrue(any("no eligible replacement" in b.lower()
                            for b in plan.blocked), plan.blocked)

    def test_out_player_never_promoted_off_bench(self):
        roster = self.base_roster()
        roster.by_platform_id("rb2").availability = Availability.OUT
        roster.by_platform_id("rbbench").availability = Availability.OUT
        plan = plan_inactive_swaps(team(), roster)
        promoted = [m.player.name for m in plan.moves if m.to_slot.is_starting]
        self.assertNotIn("RBbench", promoted)


class TestOptimal(unittest.TestCase):
    def test_promotes_higher_projection_off_bench(self):
        roster = Roster([
            mk("QB1", "QB", Slot.QB, 18),
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 4),
            mk("WR1", "WR", Slot.WR, 13),
            mk("WR2", "WR", Slot.WR, 10),
            mk("TE1", "TE", Slot.TE, 8),
            mk("FLX", "WR", Slot.FLEX, 9),
            mk("K1", "K", Slot.K, 7),
            mk("DEF1", "DEF", Slot.DEF, 6),
            mk("Stud", "RB", Slot.BENCH, 20),
        ])
        plan = plan_optimal(team(), roster)
        self.assertGreater(plan.gain, 0)
        self.assertIn("Stud", {m.player.name for m in plan.moves})

    def test_greedy_does_not_waste_flex_eligible_stud(self):
        """The classic greedy failure: a WR who could fill FLEX shouldn't be
        left benched because both WR slots got filled first."""
        roster = Roster([
            mk("QB1", "QB", Slot.QB, 18),
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 12),
            mk("WR1", "WR", Slot.WR, 16),
            mk("WR2", "WR", Slot.WR, 15),
            mk("TE1", "TE", Slot.TE, 8),
            mk("FLXlow", "TE", Slot.FLEX, 3),
            mk("K1", "K", Slot.K, 7),
            mk("DEF1", "DEF", Slot.DEF, 6),
            mk("WR3", "WR", Slot.BENCH, 14),
        ])
        plan = plan_optimal(team(), roster)
        self.assertIn("WR3", {m.player.name for m in plan.moves})
        self.assertGreater(plan.projected_after, plan.projected_before)

    def test_ir_players_are_never_started(self):
        roster = Roster([
            mk("QB1", "QB", Slot.QB, 18),
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 3),
            mk("WR1", "WR", Slot.WR, 13),
            mk("WR2", "WR", Slot.WR, 10),
            mk("TE1", "TE", Slot.TE, 8),
            mk("FLX", "WR", Slot.FLEX, 9),
            mk("K1", "K", Slot.K, 7),
            mk("DEF1", "DEF", Slot.DEF, 6),
            mk("Hurt", "RB", Slot.IR, 25, Availability.INJURED_RESERVE),
        ])
        plan = plan_optimal(team(), roster)
        self.assertNotIn("Hurt", {m.player.name for m in plan.moves})

    def test_missing_projections_do_not_crash(self):
        roster = Roster([
            mk("QB1", "QB", Slot.QB, None),
            mk("RB1", "RB", Slot.RB, None),
            mk("RB2", "RB", Slot.RB, None),
            mk("WR1", "WR", Slot.WR, None),
            mk("WR2", "WR", Slot.WR, None),
            mk("TE1", "TE", Slot.TE, None),
            mk("FLX", "WR", Slot.FLEX, None),
            mk("K1", "K", Slot.K, None),
            mk("DEF1", "DEF", Slot.DEF, None),
        ])
        plan = plan_optimal(team(), roster)
        self.assertEqual(plan.projected_after, 0.0)


class TestCrosswalk(unittest.TestCase):
    CATALOG = {
        "4034": {"full_name": "Christian McCaffrey", "position": "RB",
                 "team": "SF", "espn_id": "3117251", "yahoo_id": "30121"},
        "6794": {"first_name": "Ja'Marr", "last_name": "Chase", "position": "WR",
                 "team": "CIN", "espn_id": "4362628", "yahoo_id": "33012"},
        "CHI": {"full_name": "Chicago Bears", "position": "DEF", "team": "CHI"},
        "9999": {"full_name": "Rookie Nobody", "position": "WR", "team": "NYJ"},
    }

    def setUp(self):
        self.cw = Crosswalk.from_sleeper_catalog(self.CATALOG)

    def test_espn_id_match(self):
        p = mk("Christian McCaffrey", "RB", Slot.RB, None, pid="3117251")
        self.assertEqual(self.cw.resolve(p, "espn"), "4034")

    def test_yahoo_id_match(self):
        p = mk("Ja'Marr Chase", "WR", Slot.WR, None, pid="33012")
        self.assertEqual(self.cw.resolve(p, "yahoo"), "6794")

    def test_name_fallback_when_id_missing(self):
        p = mk("Rookie Nobody", "WR", Slot.WR, None, pid="unknown-id")
        self.assertEqual(self.cw.resolve(p, "espn"), "9999")

    def test_apostrophes_and_suffixes_normalize(self):
        self.assertEqual(normalize_name("Ja'Marr Chase"), "jamarr chase")
        self.assertEqual(normalize_name("Marvin Harrison Jr."),
                         normalize_name("Marvin Harrison"))

    def test_defense_matches_by_team_abbreviation(self):
        p = Player(platform_id="-16", name="Bears D/ST", position="DEF",
                   nfl_team="CHI", slot=Slot.DEF)
        self.assertEqual(self.cw.resolve(p, "espn"), "CHI")

    def test_unmatched_player_is_reported(self):
        players = [mk("Ghost Player", "WR", Slot.WR, None, pid="nope")]
        matched, misses = attach_projections(players, "espn", self.cw, {})
        self.assertEqual(matched, 0)
        self.assertEqual(misses, ["Ghost Player"])

    def test_projection_attaches(self):
        players = [mk("Christian McCaffrey", "RB", Slot.RB, None, pid="3117251")]
        matched, misses = attach_projections(
            players, "espn", self.cw, {"4034": 21.4})
        self.assertEqual(matched, 1)
        self.assertEqual(players[0].projection, 21.4)
        self.assertEqual(misses, [])


class TestSubPairings(unittest.TestCase):
    def test_pairs_starter_with_eligible_backup(self):
        roster = Roster([
            mk("RB1", "RB", Slot.RB, 14),
            mk("WR1", "WR", Slot.WR, 13),
            mk("RBbench", "RB", Slot.BENCH, 9),
            mk("WRbench", "WR", Slot.BENCH, 8),
        ])
        pairs = dict((s.name, b.name)
                     for s, b in sub_pairings(roster, [Slot.RB, Slot.WR]))
        self.assertEqual(pairs["RB1"], "RBbench")
        self.assertEqual(pairs["WR1"], "WRbench")

    def test_backup_not_reused_across_starters(self):
        roster = Roster([
            mk("RB1", "RB", Slot.RB, 14),
            mk("RB2", "RB", Slot.RB, 12),
            mk("OnlyBackup", "RB", Slot.BENCH, 9),
        ])
        pairs = sub_pairings(roster, [Slot.RB, Slot.RB])
        backups = [b.name for _, b in pairs]
        self.assertEqual(len(backups), len(set(backups)))
        self.assertEqual(len(pairs), 1)

    def test_injured_bench_player_is_not_offered_as_backup(self):
        roster = Roster([
            mk("RB1", "RB", Slot.RB, 14),
            mk("HurtBackup", "RB", Slot.BENCH, 9, Availability.OUT),
        ])
        self.assertEqual(sub_pairings(roster, [Slot.RB]), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
