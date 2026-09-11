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


class TestEspnEligibilityParsing(unittest.TestCase):
    """Regression: ESPN's eligibleSlots are LINEUP SLOT ids, not POSITION ids.

    Slot 5 is WR/TE; position 5 is K. Reading eligibleSlots through the position
    table made every WR kicker-eligible, and the optimizer duly suggested
    starting a wide receiver at K.
    """

    def _parse(self, default_position_id, eligible_slots):
        from ff.providers.espn import _parse_player
        entry = {
            "lineupSlotId": 20,
            "playerPoolEntry": {"player": {
                "id": 1, "fullName": "Test Player",
                "defaultPositionId": default_position_id,
                "eligibleSlots": eligible_slots,
                "proTeamId": 12, "stats": [], "injuryStatus": "ACTIVE",
            }},
        }
        return _parse_player(entry, week=1, schedule={})

    def test_wide_receiver_is_not_kicker_eligible(self):
        # A real ESPN WR: RB/WR(3), WR(4), WR/TE(5), BE(20), IR(21), FLEX(23)
        wr = self._parse(3, [3, 4, 5, 20, 21, 23])
        self.assertEqual(wr.position, "WR")
        self.assertNotIn("K", wr.eligible_positions)
        self.assertFalse(wr.can_fill(Slot.K))

    def test_wide_receiver_is_not_running_back_eligible(self):
        """FLEX eligibility says where he may be placed, not what he is."""
        wr = self._parse(3, [3, 4, 5, 20, 21, 23])
        self.assertNotIn("RB", wr.eligible_positions)
        self.assertFalse(wr.can_fill(Slot.RB))
        self.assertTrue(wr.can_fill(Slot.FLEX))
        self.assertTrue(wr.can_fill(Slot.WR))

    def test_kicker_is_kicker_eligible(self):
        k = self._parse(5, [17, 20, 21])
        self.assertEqual(k.position, "K")
        self.assertTrue(k.can_fill(Slot.K))
        self.assertFalse(k.can_fill(Slot.FLEX))

    def test_running_back_eligibility(self):
        rb = self._parse(2, [2, 3, 20, 21, 23])
        self.assertEqual(rb.position, "RB")
        self.assertTrue(rb.can_fill(Slot.RB))
        self.assertTrue(rb.can_fill(Slot.FLEX))
        self.assertFalse(rb.can_fill(Slot.K))
        self.assertFalse(rb.can_fill(Slot.WR))

    def test_dual_eligible_rb_wr_keeps_both(self):
        """A player ESPN lists for both the RB and WR slots really is both."""
        dual = self._parse(2, [2, 3, 4, 20, 21, 23])
        self.assertEqual({"RB", "WR"}, dual.eligible_positions)

    def test_defense_and_qb(self):
        d = self._parse(16, [16, 20, 21])
        self.assertEqual(d.position, "DEF")
        self.assertTrue(d.can_fill(Slot.DEF))
        self.assertFalse(d.can_fill(Slot.FLEX))
        qb = self._parse(1, [0, 20, 21])
        self.assertEqual(qb.position, "QB")
        self.assertTrue(qb.can_fill(Slot.QB))
        self.assertFalse(qb.can_fill(Slot.FLEX))

    def test_no_position_may_reach_a_foreign_single_slot(self):
        cases = {
            1: [0, 20, 21],          # QB
            2: [2, 3, 20, 21, 23],   # RB
            3: [3, 4, 5, 20, 21, 23],# WR
            4: [5, 6, 20, 21, 23],   # TE
            5: [17, 20, 21],         # K
            16: [16, 20, 21],        # DEF
        }
        for pos_id, slots in cases.items():
            p = self._parse(pos_id, slots)
            for slot in (Slot.QB, Slot.RB, Slot.WR, Slot.TE, Slot.K, Slot.DEF):
                expected = slot.value in p.eligible_positions
                self.assertEqual(p.can_fill(slot), expected,
                                 f"{p.position} vs {slot.value}")


class TestEspnActualPointsAndScheduleParsing(unittest.TestCase):
    """Regression: opponent/game_status/actual_points must actually get set.

    Player.opponent existed on the model for a long time with no provider
    ever populating it -- this pins down that ESPN's parser now does, from a
    realistic raw payload rather than hand-built Player objects.
    """

    def _parse(self, stats):
        from ff.providers.espn import _parse_player
        from ff.schedule import GameInfo
        from datetime import datetime, timezone

        entry = {
            "lineupSlotId": 20,
            "playerPoolEntry": {"player": {
                "id": 1, "fullName": "Test Player",
                "defaultPositionId": 1, "eligibleSlots": [0, 20, 21],
                "proTeamId": 12,  # KC
                "stats": stats, "injuryStatus": "ACTIVE",
            }},
        }
        schedule = {"KC": GameInfo(
            kickoff=datetime(2026, 9, 14, tzinfo=timezone.utc),
            opponent="LV", status="Final", state="post")}
        return _parse_player(entry, week=1, schedule=schedule)

    def test_opponent_and_game_status_come_from_schedule(self):
        p = self._parse(stats=[])
        self.assertEqual(p.opponent, "LV")
        self.assertEqual(p.game_status, "Final")

    def test_actual_points_read_from_statsourceid_zero(self):
        p = self._parse(stats=[
            {"scoringPeriodId": 1, "statSourceId": 1, "statSplitTypeId": 1,
             "appliedTotal": 18.4},   # projected
            {"scoringPeriodId": 1, "statSourceId": 0, "statSplitTypeId": 1,
             "appliedTotal": 22.7},   # actual
        ])
        self.assertEqual(p.projection, 18.4)
        self.assertEqual(p.actual_points, 22.7)

    def test_actual_points_none_when_no_actual_stat_row(self):
        p = self._parse(stats=[
            {"scoringPeriodId": 1, "statSourceId": 1, "statSplitTypeId": 1,
             "appliedTotal": 18.4},
        ])
        self.assertIsNone(p.actual_points)


class TestPositionSlotTruthTable(unittest.TestCase):
    """The complete legality matrix, asserted explicitly.

    QB  -> QB, SUPERFLEX
    RB  -> RB, FLEX, WRRB, SUPERFLEX
    WR  -> WR, FLEX, WRRB, WRTE, SUPERFLEX
    TE  -> TE, FLEX, WRTE, SUPERFLEX
    K   -> K
    DEF -> DEF
    (BENCH and IR accept anyone.)
    """

    EXPECTED = {
        "QB":  {Slot.QB, Slot.SUPERFLEX},
        "RB":  {Slot.RB, Slot.FLEX, Slot.WRRB, Slot.SUPERFLEX},
        "WR":  {Slot.WR, Slot.FLEX, Slot.WRRB, Slot.WRTE, Slot.SUPERFLEX},
        "TE":  {Slot.TE, Slot.FLEX, Slot.WRTE, Slot.SUPERFLEX},
        "K":   {Slot.K},
        "DEF": {Slot.DEF},
    }

    def test_every_position_against_every_slot(self):
        for pos, legal_starting in self.EXPECTED.items():
            p = mk(f"{pos}-guy", pos, Slot.BENCH, 10)
            expected = legal_starting | {Slot.BENCH, Slot.IR}
            self.assertEqual(
                p.legal_slots, expected,
                f"{pos} legal slots wrong: got {sorted(s.value for s in p.legal_slots)}, "
                f"want {sorted(s.value for s in expected)}")

    def test_wr_cannot_fill_te_qb_k_or_def(self):
        wr = mk("Receiver", "WR", Slot.BENCH, 15)
        for forbidden in (Slot.TE, Slot.QB, Slot.K, Slot.DEF):
            self.assertFalse(wr.can_fill(forbidden),
                             f"WR must not fill {forbidden.value}")

    def test_rb_only_rb_and_flex_family(self):
        rb = mk("Runner", "RB", Slot.BENCH, 15)
        self.assertTrue(rb.can_fill(Slot.RB))
        self.assertTrue(rb.can_fill(Slot.FLEX))
        for forbidden in (Slot.WR, Slot.TE, Slot.QB, Slot.K, Slot.DEF):
            self.assertFalse(rb.can_fill(forbidden))

    def test_qb_only_qb_and_superflex(self):
        qb = mk("Passer", "QB", Slot.BENCH, 22)
        self.assertTrue(qb.can_fill(Slot.QB))
        self.assertTrue(qb.can_fill(Slot.SUPERFLEX))
        for forbidden in (Slot.FLEX, Slot.RB, Slot.WR, Slot.TE, Slot.K,
                          Slot.DEF, Slot.WRRB, Slot.WRTE):
            self.assertFalse(qb.can_fill(forbidden))

    def test_te_only_te_and_flex_family(self):
        te = mk("TightEnd", "TE", Slot.BENCH, 12)
        self.assertTrue(te.can_fill(Slot.TE))
        self.assertTrue(te.can_fill(Slot.FLEX))
        self.assertTrue(te.can_fill(Slot.WRTE))
        for forbidden in (Slot.WR, Slot.RB, Slot.QB, Slot.K, Slot.DEF, Slot.WRRB):
            self.assertFalse(te.can_fill(forbidden))

    def test_kicker_and_defense_are_isolated(self):
        k = mk("Kicker", "K", Slot.BENCH, 9)
        d = mk("Defense", "DEF", Slot.BENCH, 8)
        for slot in Slot:
            if slot in (Slot.BENCH, Slot.IR):
                continue
            self.assertEqual(k.can_fill(slot), slot is Slot.K)
            self.assertEqual(d.can_fill(slot), slot is Slot.DEF)


class TestEligibilitySanitization(unittest.TestCase):
    """Garbage from a provider must never widen a player's eligibility."""

    def test_junk_positions_are_discarded(self):
        p = Player(platform_id="1", name="X", position="WR",
                   eligible_positions={"WR", "NONSENSE", "P", "LB"})
        self.assertEqual(p.eligible_positions, {"WR"})

    def test_provider_claiming_wr_is_kicker_eligible_is_overruled(self):
        """Exactly the ESPN bug, blocked a second time at the model layer."""
        p = Player(platform_id="1", name="X", position="WR",
                   eligible_positions={"WR", "K", "DEF"})
        self.assertEqual(p.eligible_positions, {"WR"})
        self.assertFalse(p.can_fill(Slot.K))
        self.assertFalse(p.can_fill(Slot.DEF))

    def test_kicker_cannot_be_widened(self):
        p = Player(platform_id="1", name="K", position="K",
                   eligible_positions={"K", "WR", "RB", "TE"})
        self.assertEqual(p.eligible_positions, {"K"})
        self.assertFalse(p.can_fill(Slot.FLEX))

    def test_real_dual_eligibility_survives(self):
        rbwr = Player(platform_id="1", name="Dual", position="RB",
                      eligible_positions={"RB", "WR"})
        self.assertEqual(rbwr.eligible_positions, {"RB", "WR"})
        hill = Player(platform_id="2", name="Taysom Hill", position="QB",
                      eligible_positions={"QB", "TE"})
        self.assertEqual(hill.eligible_positions, {"QB", "TE"})
        self.assertTrue(hill.can_fill(Slot.TE))
        self.assertTrue(hill.can_fill(Slot.QB))

    def test_dst_spellings_normalize_to_def(self):
        for spelling in ("DST", "D/ST", "DEF"):
            p = Player(platform_id="1", name="D", position=spelling)
            self.assertEqual(p.position, "DEF")
            self.assertTrue(p.can_fill(Slot.DEF))

    def test_primary_position_always_included(self):
        p = Player(platform_id="1", name="X", position="TE",
                   eligible_positions=set())
        self.assertIn("TE", p.eligible_positions)


class TestPlanValidation(unittest.TestCase):
    def test_illegal_move_raises(self):
        from ff.models import IllegalLineup
        from ff.optimizer import validate_plan
        wr = mk("Receiver", "WR", Slot.BENCH, 15)
        plan = LineupPlanStub(moves=[Move(player=wr, from_slot=Slot.BENCH,
                                          to_slot=Slot.K)])
        with self.assertRaises(IllegalLineup):
            validate_plan(plan)

    def test_benching_anyone_is_legal(self):
        from ff.optimizer import validate_plan
        k = mk("Kicker", "K", Slot.K, 9)
        plan = LineupPlanStub(moves=[Move(player=k, from_slot=Slot.K,
                                          to_slot=Slot.BENCH)])
        validate_plan(plan)  # must not raise

    def test_optimizer_never_emits_illegal_lineup_under_fuzz(self):
        """Random rosters, many times over: no plan may contain an illegal slot."""
        import random
        from ff.models import IllegalLineup
        rng = random.Random(20260910)
        positions = ["QB", "RB", "WR", "TE", "K", "DEF"]
        for _ in range(400):
            players = []
            for i in range(rng.randint(9, 18)):
                pos = rng.choice(positions)
                players.append(mk(f"P{i}", pos,
                                  rng.choice(list(Slot)),
                                  rng.choice([None, 0.0, rng.uniform(0, 30)]),
                                  rng.choice([Availability.ACTIVE,
                                              Availability.OUT,
                                              Availability.QUESTIONABLE,
                                              Availability.BYE]),
                                  pid=f"p{i}"))
            roster = Roster(players)
            try:
                for plan in (plan_optimal(team(), roster),
                             plan_inactive_swaps(team(), roster)):
                    for m in plan.moves:
                        if m.to_slot.is_starting:
                            self.assertTrue(
                                m.player.can_fill(m.to_slot),
                                f"{m.player.position} -> {m.to_slot.value}")
            except IllegalLineup as e:
                self.fail(f"optimizer produced an illegal lineup: {e}")


from ff.models import LineupPlan as _LP, Move


def LineupPlanStub(moves):
    return _LP(team=team(), moves=moves, projected_before=0.0,
               projected_after=0.0)
