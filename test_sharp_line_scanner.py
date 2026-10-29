"""
Tests for sharp_line_scanner.py. Run them with:

    python3 -m unittest -v
"""

import contextlib
import io
import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import sharp_line_scanner as scanner

NOW = datetime(2026, 10, 27, 18, 0, tzinfo=timezone.utc)


def make_event(pinnacle_markets, soft_markets, commence="2026-10-30T00:00:00Z",
               pinnacle_time="2026-10-27T17:55:00Z", soft_time="2026-10-27T17:56:00Z"):
    """Build a small fake event with Pinnacle and DraftKings."""
    def book(key, title, markets, time):
        return {"key": key, "title": title, "last_update": time,
                "markets": [{"key": k, "last_update": time, "outcomes": o} for k, o in markets.items()]}

    return {
        "id": "test", "commence_time": commence, "home_team": "Home", "away_team": "Away",
        "bookmakers": [book("pinnacle", "Pinnacle", pinnacle_markets, pinnacle_time),
                       book("draftkings", "DraftKings", soft_markets, soft_time)],
    }


TOTALS_PINNACLE = {"totals": [{"name": "Over", "price": scanner.american_to_decimal(-120), "point": 47.5},
                              {"name": "Under", "price": 2.00, "point": 47.5}]}
TOTALS_SOFT = {"totals": [{"name": "Over", "price": 2.05, "point": 47.5}]}


class OddsMathTests(unittest.TestCase):

    def test_american_conversion(self):
        self.assertEqual(scanner.decimal_to_american(2.05), 105)
        self.assertEqual(scanner.decimal_to_american(1.8333), -120)
        self.assertAlmostEqual(scanner.american_to_decimal(-120), 1.8333, places=4)
        self.assertAlmostEqual(scanner.american_to_decimal(150), 2.50)

    def test_remove_vig_worked_example(self):
        # Pinnacle Over -120 / Under +100: 54.55% + 50.00% = 104.55%.
        # Each side gives up half the 4.55-point margin -> fair Over 52.27%.
        over, under = scanner.remove_vig([scanner.american_to_decimal(-120), 2.00])
        self.assertAlmostEqual(over, 0.5227, places=4)
        self.assertAlmostEqual(over + under, 1.0)

    def test_shin_equals_additive_for_two_sides(self):
        # The Kings game: Pinnacle +927 / -1430. Additive gives the Kings 8.13%.
        kings, spurs = scanner.remove_vig([10.27, 1.0698])
        implied_kings, implied_spurs = 1 / 10.27, 1 / 1.0698
        margin = implied_kings + implied_spurs - 1
        self.assertAlmostEqual(kings, implied_kings - margin / 2, places=6)
        self.assertLess(scanner.expected_value(kings, 11.40), 0)   # FanDuel +1040 is not +EV

    def test_shin_handles_three_sides_and_extreme_longshots(self):
        probs = scanner.remove_vig([1.25, 6.50, 34.0])   # big favorite, draw, huge longshot
        self.assertAlmostEqual(sum(probs), 1.0)
        self.assertTrue(all(p > 0 for p in probs))

    def test_expected_value_worked_example(self):
        # Fair Over 52.27% at +105 -> EV about +7.2%
        self.assertAlmostEqual(scanner.expected_value(0.5227, 2.05), 0.0715, places=3)

    def test_stake_grows_with_edge(self):
        small = scanner.suggested_stake_pct(0.51, 2.0)   # 2% EV
        large = scanner.suggested_stake_pct(0.53, 2.0)   # 6% EV
        self.assertAlmostEqual(small, 0.5)               # quarter of 2% Kelly
        self.assertGreater(large, small)

    def test_stake_is_zero_without_edge_and_capped(self):
        self.assertEqual(scanner.suggested_stake_pct(0.45, 2.0), 0.0)
        self.assertEqual(scanner.suggested_stake_pct(0.90, 2.0), scanner.MAX_STAKE_PCT)


class FindEdgesTests(unittest.TestCase):

    def test_finds_worked_example(self):
        edges = scanner.find_edges(make_event(TOTALS_PINNACLE, TOTALS_SOFT), "NFL", NOW, 2.0)
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["bet"], "Over 47.5")
        self.assertEqual(edges[0]["odds"], 105)
        self.assertEqual(edges[0]["fair_odds"], -110)
        self.assertAlmostEqual(edges[0]["ev_pct"], 7.16, places=1)

    def test_skips_different_line(self):
        soft = {"totals": [{"name": "Over", "price": 2.05, "point": 48.5}]}
        self.assertEqual(scanner.find_edges(make_event(TOTALS_PINNACLE, soft), "NFL", NOW, 2.0), [])

    def test_skips_below_minimum_ev(self):
        self.assertEqual(scanner.find_edges(make_event(TOTALS_PINNACLE, TOTALS_SOFT), "NFL", NOW, 8.0), [])

    def test_skips_started_game(self):
        event = make_event(TOTALS_PINNACLE, TOTALS_SOFT, commence="2026-10-27T17:00:00Z")
        self.assertEqual(scanner.find_edges(event, "NFL", NOW, 2.0), [])

    def test_skips_stale_pinnacle_price(self):
        # Soft book updated 10 minutes after Pinnacle
        event = make_event(TOTALS_PINNACLE, TOTALS_SOFT,
                           pinnacle_time="2026-10-27T17:45:00Z", soft_time="2026-10-27T17:55:00Z")
        self.assertEqual(scanner.find_edges(event, "NFL", NOW, 2.0), [])

    def test_spread_sides_are_paired(self):
        pinnacle = {"spreads": [{"name": "Home", "price": 1.95, "point": -2.5},
                                {"name": "Away", "price": 1.93, "point": 2.5}]}
        soft = {"spreads": [{"name": "Away", "price": 2.10, "point": 2.5}]}
        edges = scanner.find_edges(make_event(pinnacle, soft), "NFL", NOW, 2.0)
        self.assertEqual([e["bet"] for e in edges], ["Away +2.5"])

    def test_props_match_on_player(self):
        pinnacle = {"player_points": [
            {"name": "Over", "description": "Player A", "price": 1.95, "point": 27.5},
            {"name": "Under", "description": "Player A", "price": 1.87, "point": 27.5},
            {"name": "Over", "description": "Player B", "price": 1.90, "point": 27.5},
            {"name": "Under", "description": "Player B", "price": 1.90, "point": 27.5}]}
        soft = {"player_points": [{"name": "Under", "description": "Player A", "price": 2.08, "point": 27.5}]}
        edges = scanner.find_edges(make_event(pinnacle, soft), "NBA", NOW, 2.0)
        self.assertEqual([e["bet"] for e in edges], ["Player A Under 27.5"])

    def test_skips_one_sided_pinnacle_price(self):
        # Only "Yes" is offered, so the vig can't be removed
        pinnacle = {"player_anytime_td": [{"name": "Yes", "description": "Player A", "price": 2.50}]}
        soft = {"player_anytime_td": [{"name": "Yes", "description": "Player A", "price": 3.50}]}
        self.assertEqual(scanner.find_edges(make_event(pinnacle, soft), "NFL", NOW, 2.0), [])


class DemoScanTests(unittest.TestCase):

    def test_demo_scan_finds_expected_bets(self):
        edges, _ = scanner.run_scan(scanner.DemoOdds(), list(scanner.SPORTS), scanner.GAME_MARKETS.split(","),
                                    include_props=True, max_prop_games=3, min_ev=2.0)
        found = {(e["book"], e["bet"]) for e in edges}
        self.assertEqual(len(edges), 8)
        self.assertIn(("DraftKings", "Over 47.5"), found)
        self.assertNotIn(("BetMGM", "Under 47.5"), found)            # stale Pinnacle price
        self.assertNotIn(("DraftKings", "Dallas Cowboys to win"), found)  # game started
        self.assertEqual(edges, sorted(edges, key=lambda e: e["ev_pct"], reverse=True))

    def test_tennis_scans_moneyline_only_by_default(self):
        requested = {}

        class RecordingDemo(scanner.DemoOdds):
            def game_odds(self, sport_key, markets):
                requested[sport_key] = markets
                return super().game_odds(sport_key, markets)

        scanner.run_scan(RecordingDemo(), ["nba", "tennis"], None,
                         include_props=False, max_prop_games=0, min_ev=2.0)
        self.assertEqual(requested["basketball_nba"], ["h2h", "spreads", "totals"])
        self.assertEqual(requested["tennis_atp_swiss_indoors"], ["h2h"])


def grade(market_key, outcome, point, scores):
    row = {"market_key": market_key, "outcome": outcome, "point": "" if point is None else str(point)}
    return scanner.grade_bet(row, scores)


class PaperTradingTests(unittest.TestCase):

    def setUp(self):
        folder = tempfile.mkdtemp()
        self.log = os.path.join(folder, "paper_bets.csv")
        self.addCleanup(shutil.rmtree, folder)

    def demo_scan(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return scanner.run_scan(scanner.DemoOdds(), ["nba", "tennis"], None,
                                    include_props=False, max_prop_games=0, min_ev=2.0)

    def test_grading(self):
        nba = {"Boston Celtics": 110, "New York Knicks": 104}
        self.assertEqual(grade("h2h", "New York Knicks", None, nba), "lost")
        self.assertEqual(grade("spreads", "New York Knicks", 6.5, nba), "won")
        self.assertEqual(grade("spreads", "New York Knicks", 6.0, nba), "push")
        self.assertEqual(grade("totals", "Under", 221.5, nba), "won")
        self.assertEqual(grade("h2h", "Casper Ruud", None, {"Casper Ruud": 2, "Other Player": 1}), "won")
        self.assertIsNone(grade("player_points", "Over", 27.5, nba))

    def test_profit(self):
        self.assertAlmostEqual(scanner.bet_profit("won", 2.50, 10), 15.0)
        self.assertEqual(scanner.bet_profit("lost", 2.50, 10), -10)
        self.assertEqual(scanner.bet_profit("push", 2.50, 10), 0.0)

    def test_logs_each_bet_once_and_tracks_close(self):
        edges, sharp_prices = self.demo_scan()
        now = scanner.DemoOdds().now()
        self.assertEqual(scanner.log_paper_bets(edges, sharp_prices, now, self.log)[0], 2)
        self.assertEqual(scanner.log_paper_bets(edges, sharp_prices, now, self.log)[0], 0)

        # Pinnacle moves toward the Knicks bet (+150 at Fanatics): the close is now 45% fair.
        knicks = next(e["bet_id"] for e in edges if e["outcome"] == "New York Knicks")
        later = now + timedelta(hours=1)
        scanner.log_paper_bets([], {knicks: (0.45, 2.15)}, later, self.log)
        row = next(r for r in scanner.load_paper_log(self.log) if r["bet_id"] == knicks)
        self.assertAlmostEqual(float(row["clv_pct"]), 12.5)   # 0.45 x 2.50 - 1

    def test_close_check_fetches_only_games_starting_soon(self):
        edges, sharp_prices = self.demo_scan()
        scanner.log_paper_bets(edges, sharp_prices, scanner.DemoOdds().now(), self.log)

        # The NBA game starts at 23:30 UTC. At 23:25 Pinnacle has moved to the Knicks.
        nba_game = scanner.DemoOdds().game_odds("basketball_nba", ["h2h"])[0]
        pinnacle = nba_game["bookmakers"][0]
        pinnacle["markets"][0]["outcomes"] = [{"name": "Boston Celtics", "price": 1.75},
                                              {"name": "New York Knicks", "price": 2.20}]

        class FakeCloseSource:
            def __init__(self):
                self.requests = []

            def now(self):
                return datetime(2026, 10, 27, 23, 25, tzinfo=timezone.utc)

            def event_odds(self, sport_key, event_id, markets):
                self.requests.append((event_id, markets))
                return nba_game

            def summary(self):
                return ""

        source = FakeCloseSource()
        with contextlib.redirect_stdout(io.StringIO()):
            scanner.capture_closes(source, self.log)
            scanner.capture_closes(source, self.log)   # second run: price just recorded
        self.assertEqual(source.requests, [("demo-nba-1", ["h2h"])])   # tennis isn't due yet

        rows = {r["outcome"]: r for r in scanner.load_paper_log(self.log)}
        self.assertEqual(rows["New York Knicks"]["close_pinnacle_odds"], "120")
        # The line moved our way, so our +150 beat the close: 44.16% fair x 2.50 - 1 = +10.4%
        self.assertAlmostEqual(float(rows["New York Knicks"]["clv_pct"]), 10.39)

        # Only the NBA bet has a close seen after it was logged.
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.paper_report(self.log)
        self.assertIn("Closing line value (1 bets", output.getvalue())

    def test_dashboard_contains_scan_and_paper_bets(self):
        edges, sharp_prices = self.demo_scan()
        now = scanner.DemoOdds().now()
        scanner.log_paper_bets(edges, sharp_prices, now, self.log)
        scan_path = os.path.join(os.path.dirname(self.log), "latest_scan.json")
        page_path = os.path.join(os.path.dirname(self.log), "dashboard.html")
        edges[0]["bet"] = "</script><b>sneaky</b>"   # text that could break the page
        scanner.save_latest_scan(edges, now, True, 2.0, scan_path)
        scanner.build_dashboard("perf", scan_path, self.log, page_path)

        with open(page_path) as file:
            page = file.read()
        self.assertNotIn("__DATA__", page)
        self.assertNotIn("</script><b>", page)        # escaped, so the script tag stays intact
        self.assertIn("Mateo Ruiz to win", page)      # from the scan and the paper log
        self.assertIn('"default_tab": "perf"', page)

    def test_settle_and_report(self):
        edges, sharp_prices = self.demo_scan()
        scanner.log_paper_bets(edges, sharp_prices, scanner.DemoOdds().now(), self.log)

        class FakeScores:
            def now(self):
                return datetime(2026, 10, 29, tzinfo=timezone.utc)

            def scores(self, sport_key):
                return [
                    {"id": "demo-nba-1", "completed": True, "scores": [
                        {"name": "Boston Celtics", "score": "104"},
                        {"name": "New York Knicks", "score": "110"}]},
                    {"id": "demo-tennis-1", "completed": True, "scores": [
                        {"name": "Alex Carter", "score": "2"},
                        {"name": "Mateo Ruiz", "score": "0"}]},
                ]

        with contextlib.redirect_stdout(io.StringIO()):
            scanner.settle_paper_bets(FakeScores(), self.log)
        rows = {r["outcome"]: r for r in scanner.load_paper_log(self.log)}
        self.assertEqual(rows["New York Knicks"]["status"], "won")
        self.assertAlmostEqual(float(rows["New York Knicks"]["flat_profit"]), 1.5)
        self.assertEqual(rows["Mateo Ruiz"]["status"], "lost")

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            scanner.paper_report(self.log)
        self.assertIn("Results: 1-1-0", output.getvalue())


if __name__ == "__main__":
    unittest.main()
