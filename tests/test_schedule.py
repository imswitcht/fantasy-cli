import unittest
from datetime import datetime, timezone

from ff.models import Availability, Slot
from ff.schedule import GameInfo, normalize_team_abbr


class TestNormalizeTeamAbbr(unittest.TestCase):
    def test_was_aliases_to_wsh(self):
        self.assertEqual(normalize_team_abbr("WAS"), "WSH")
        self.assertEqual(normalize_team_abbr("was"), "WSH")

    def test_unmapped_abbr_passes_through_uppercased(self):
        self.assertEqual(normalize_team_abbr("sea"), "SEA")

    def test_none_and_empty_pass_through(self):
        self.assertIsNone(normalize_team_abbr(None))
        self.assertEqual(normalize_team_abbr(""), "")


class TestSleeperWashingtonByeRegression(unittest.TestCase):
    """The bug: Sleeper's own player catalog uses "WAS" for Washington, but
    the shared schedule dict (built from ESPN's public scoreboard) uses
    "WSH". Every Washington player on a Sleeper roster was landing on the
    "no schedule entry for this team -> bye" fallback, even in week 1.
    """

    def test_washington_player_not_marked_bye_in_week_one(self):
        from ff.providers.sleeper import _build

        catalog = {"999": {
            "position": "QB", "team": "WAS", "full_name": "Test Commander",
            "fantasy_positions": ["QB"],
        }}
        schedule = {"WSH": GameInfo(
            kickoff=datetime(2026, 9, 14, tzinfo=timezone.utc),
            opponent="NYG", status="Sun 1:00 PM", state="pre", score="")}

        p = _build("999", catalog, proj={}, actual={}, schedule=schedule, slot=Slot.QB)

        self.assertIsNotNone(p)
        self.assertEqual(p.nfl_team, "WSH")
        self.assertIsNot(p.availability, Availability.BYE)
        self.assertEqual(p.opponent, "NYG")


if __name__ == "__main__":
    unittest.main(verbosity=2)
