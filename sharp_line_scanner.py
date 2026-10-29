"""
Sharp Line Scanner
==================
Finds positive expected value (+EV) sports bets by comparing soft sportsbook
prices to Pinnacle's "sharp" line.

How it works
------------
1. Download current odds from The Odds API (the-odds-api.com, v4).
2. Treat Pinnacle as the true price and remove its vig (the bookmaker's
   built-in margin) with Shin's method.
3. Compare every soft book's price on the exact same bet
   (same market, same outcome, same player, same line).
4. EV = fair probability x decimal odds - 1. Report every bet at or above the
   minimum EV with a suggested stake, and save them to a timestamped CSV.

This tool only finds and reports prices. It never places bets.

Usage
-----
    python3 sharp_line_scanner.py --demo
    python3 sharp_line_scanner.py
    python3 sharp_line_scanner.py --sports nfl,nba --props --bankroll 500
"""

import argparse
import copy
import csv
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

# Your Odds API key. Leave this blank and set the ODDS_API_KEY environment
# variable instead, so the key never ends up inside the code.
API_KEY = ""

API_BASE = "https://api.the-odds-api.com/v4"

# The sharp book whose prices we treat as the "true" odds.
SHARP_BOOK = "pinnacle"

# Soft books legal in Ohio, by their Odds API key. Pinnacle plus up to 9 soft
# books counts as one "region", so each market costs 1 credit per request.
SOFT_BOOKS = [
    "draftkings",
    "fanduel",
    "betmgm",
    "williamhill_us",   # Caesars
    "espnbet",
    "fanatics",
    "betrivers",
    "hardrockbet_oh",   # Hard Rock Bet's Ohio prices
]

# Short names used on the command line -> Odds API sport keys.
# Tennis has no single key: every tournament is its own "sport", so the
# scanner looks up whichever tournaments are active at the time.
SPORTS = {
    "nfl": "americanfootball_nfl",
    "cfb": "americanfootball_ncaaf",
    "nba": "basketball_nba",
    "tennis": None,
}

# Sports scanned when --sports isn't given. NFL and CFB are available but off for now.
DEFAULT_SPORTS = "nba,tennis"

GAME_MARKETS = "h2h,spreads,totals"

# Sports that scan fewer game markets to save credits. Every tennis
# tournament costs credits separately, so tennis checks moneylines only.
SPORT_MARKETS = {
    "tennis": "h2h",
}

# Player prop markets per sport. College football is left out on purpose:
# Ohio does not allow player props on college athletes. The API does not
# offer tennis player props.
PROP_MARKETS = {
    "nfl": ["player_pass_yds", "player_rush_yds", "player_reception_yds", "player_receptions"],
    "nba": ["player_points", "player_rebounds", "player_assists", "player_threes"],
}

MARKET_LABELS = {
    "h2h": "Moneyline",
    "spreads": "Spread",
    "totals": "Total",
    "player_pass_yds": "Pass yds",
    "player_rush_yds": "Rush yds",
    "player_reception_yds": "Rec yds",
    "player_receptions": "Receptions",
    "player_points": "Points",
    "player_rebounds": "Rebounds",
    "player_assists": "Assists",
    "player_threes": "3-pointers",
}

DEFAULT_MIN_EV = 2.0           # percent
DEFAULT_MAX_CREDITS = 50       # the most credits one run is allowed to spend
DEFAULT_MAX_PROP_GAMES = 3     # games per sport to pull player props for

# If a soft book's price was updated more than this many seconds AFTER
# Pinnacle's, Pinnacle's price may be out of date, so we skip the comparison.
MAX_STALE_SECONDS = 5 * 60

# Bet sizing: a fraction of the Kelly criterion, capped at a maximum.
KELLY_FRACTION = 0.25
MAX_STAKE_PCT = 2.5            # percent of bankroll

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEMO_FILE = os.path.join(SCRIPT_DIR, "demo_odds.json")

RECHECK_REMINDER = "Prices move fast. Recheck every price in the sportsbook app before betting."

# Paper trading: every real scan logs its edges here as pretend bets.
PAPER_LOG_FILE = os.path.join(SCRIPT_DIR, "paper_bets.csv")
PAPER_BANKROLL = 1000.0        # starting paper bankroll in dollars, for Kelly stakes
SCORES_DAYS_BACK = 3           # the API only returns results up to 3 days old
SETTLEABLE_MARKETS = {"h2h", "spreads", "totals"}

# --close fetches games starting within this many minutes. GitHub Actions asks
# for a run every 10 minutes but often starts it late, so the window is wide and
# each run refreshes the price: the last run before the start sets the close.
CLOSE_WINDOW_MINUTES = 30
# Skip a game if its price was already recorded this recently (saves credits).
CLOSE_REFRESH_MINUTES = 5

# The dashboard: an HTML page built from the latest scan and the paper log.
LATEST_SCAN_FILE = os.path.join(SCRIPT_DIR, "latest_scan.json")
DASHBOARD_TEMPLATE = os.path.join(SCRIPT_DIR, "dashboard_template.html")
DASHBOARD_FILE = os.path.join(SCRIPT_DIR, "dashboard.html")

PAPER_COLUMNS = [
    "bet_id", "logged_at", "sport", "sport_key", "event_id", "game", "starts",
    "market_key", "market", "outcome", "player", "point", "bet", "book",
    "odds", "decimal_odds", "fair_prob", "ev_pct", "stake_pct", "kelly_stake",
    "close_pinnacle_odds", "close_fair_prob", "close_seen_at", "clv_pct",
    "status", "final_score", "flat_profit", "kelly_profit",
]


# ---------------------------------------------------------------------------
# Odds math
# ---------------------------------------------------------------------------

def decimal_to_american(decimal_odds):
    """Convert decimal odds (2.05) to American odds (+105)."""
    if decimal_odds >= 2.0:
        return round((decimal_odds - 1) * 100)
    return round(-100 / (decimal_odds - 1))


def american_to_decimal(american_odds):
    """Convert American odds (-120) to decimal odds (1.833)."""
    if american_odds > 0:
        return 1 + american_odds / 100
    return 1 + 100 / -american_odds


def format_american(american_odds):
    """Show American odds with a sign, e.g. +105 or -110."""
    return f"{american_odds:+d}"


def remove_vig(decimal_prices):
    """
    Turn one bet's prices (all sides) into fair probabilities that add to 100%,
    using Shin's method.

    Bookmakers hide more of their margin in underdog prices (the
    favorite-longshot bias), so simply scaling every side down by the same
    percentage overrates underdogs. Shin's model assumes a share z of the
    money comes from bettors with inside information and finds the z that
    makes the probabilities add to 100%. For a two-sided bet it gives the same
    answer as the additive method: each side gives up an equal number of
    percentage points. Example: -120 / +100 is 54.5% + 50.0% = 104.5%, so
    each side loses 2.27 points and the fair Over is 52.3%.
    """
    implied = [1 / price for price in decimal_prices]
    total = sum(implied)
    if total <= 1:
        return [prob / total for prob in implied]  # no margin to remove

    def shin_probs(z):
        return [(math.sqrt(z * z + 4 * (1 - z) * q * q / total) - z) / (2 * (1 - z))
                for q in implied]

    # The probabilities shrink as z grows, so home in on the z where they total 1.
    low, high = 0.0, 0.99
    for _ in range(100):
        z = (low + high) / 2
        if sum(shin_probs(z)) > 1:
            low = z
        else:
            high = z
    return shin_probs((low + high) / 2)


def expected_value(fair_prob, decimal_odds):
    """Expected profit per $1 bet. 0.07 means +7%."""
    return fair_prob * decimal_odds - 1


def suggested_stake_pct(fair_prob, decimal_odds):
    """
    Suggested bet size as a percent of bankroll, using fractional Kelly.

    Full Kelly = EV / (decimal odds - 1). A bigger edge means a bigger stake,
    and longer odds mean a smaller one (more variance). Full Kelly is very
    aggressive, so we bet a quarter of it and cap the result at MAX_STAKE_PCT.
    """
    ev = expected_value(fair_prob, decimal_odds)
    if ev <= 0:
        return 0.0
    full_kelly = ev / (decimal_odds - 1)
    return min(full_kelly * KELLY_FRACTION * 100, MAX_STAKE_PCT)


# ---------------------------------------------------------------------------
# Comparing books
# ---------------------------------------------------------------------------

def parse_time(text):
    """Turn the API's time text ("2026-10-30T00:15:00Z") into a datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def bet_key(market_key, outcome):
    """Identifies one exact bet: same market, outcome, player and line."""
    return (market_key, outcome["name"], outcome.get("description"), outcome.get("point"))


def make_bet_id(event_id, market_key, outcome_name, player, point):
    """One text ID per exact bet in one game, used to find it again in the paper log."""
    parts = [event_id, market_key, outcome_name, player or "", "" if point is None else point]
    return "|".join(str(part) for part in parts)


def side_group(market_key, outcome):
    """
    Identifies which outcomes are opposite sides of the same bet, so their
    fair probabilities add to 100%. For spreads, Chiefs -2.5 and Bills +2.5
    belong together, which is why the line is compared without its sign.
    """
    point = outcome.get("point")
    return (market_key, outcome.get("description"), abs(point) if point is not None else None)


def pinnacle_fair_prices(pinnacle):
    """Return {bet_key: (fair probability, Pinnacle price, last update)}."""
    fair = {}
    for market in pinnacle["markets"]:
        updated = parse_time(market.get("last_update", pinnacle["last_update"]))

        groups = {}
        for outcome in market["outcomes"]:
            groups.setdefault(side_group(market["key"], outcome), []).append(outcome)

        for sides in groups.values():
            # A one-sided price (e.g. only "Yes") can't have its vig removed.
            if len(sides) < 2:
                continue
            probs = remove_vig([outcome["price"] for outcome in sides])
            for outcome, prob in zip(sides, probs):
                fair[bet_key(market["key"], outcome)] = (prob, outcome["price"], updated)
    return fair


def describe_bet(market_key, outcome):
    """A readable name for the bet, e.g. "Over 47.5" or "Chiefs -2.5"."""
    name = outcome["name"]
    point = outcome.get("point")
    player = outcome.get("description")
    if market_key == "h2h":
        return f"{name} to win"
    if market_key == "spreads":
        return f"{name} {point:+g}"
    if market_key == "totals":
        return f"{name} {point:g}"
    if point is None:
        return f"{player} {name}"
    return f"{player} {name} {point:g}"


def find_edges(event, sport, now, min_ev):
    """Compare every soft book in one event to Pinnacle and return the +EV bets."""
    starts = parse_time(event["commence_time"])
    if starts <= now:
        return []  # game already started

    books = {book["key"]: book for book in event.get("bookmakers", [])}
    if SHARP_BOOK not in books:
        return []
    fair = pinnacle_fair_prices(books[SHARP_BOOK])

    edges = []
    for book_key in SOFT_BOOKS:
        book = books.get(book_key)
        if book is None:
            continue
        for market in book["markets"]:
            soft_updated = parse_time(market.get("last_update", book["last_update"]))
            for outcome in market["outcomes"]:
                key = bet_key(market["key"], outcome)
                if key not in fair:
                    continue  # Pinnacle doesn't have this exact line

                fair_prob, sharp_price, sharp_updated = fair[key]
                if (soft_updated - sharp_updated).total_seconds() > MAX_STALE_SECONDS:
                    continue  # Pinnacle's price may be out of date

                price = outcome["price"]
                ev_pct = expected_value(fair_prob, price) * 100
                if ev_pct < min_ev:
                    continue

                edges.append({
                    "bet_id": make_bet_id(event["id"], *key),
                    "event_id": event["id"],
                    "sport_key": event.get("sport_key", ""),
                    "market_key": market["key"],
                    "outcome": outcome["name"],
                    "player": outcome.get("description"),
                    "point": outcome.get("point"),
                    "ev_pct": ev_pct,
                    "stake_pct": suggested_stake_pct(fair_prob, price),
                    "sport": sport,
                    "game": f"{event['away_team']} @ {event['home_team']}",
                    "starts": starts,
                    "market": MARKET_LABELS.get(market["key"], market["key"]),
                    "bet": describe_bet(market["key"], outcome),
                    "book": book["title"],
                    "odds": decimal_to_american(price),
                    "fair_odds": decimal_to_american(1 / fair_prob),
                    "pinnacle_odds": decimal_to_american(sharp_price),
                    "decimal_odds": price,
                    "fair_prob": fair_prob,
                })
    return edges


def current_sharp_prices(event, now):
    """Pinnacle's current fair price for every bet in an upcoming game, keyed by bet ID."""
    books = {book["key"]: book for book in event.get("bookmakers", [])}
    if SHARP_BOOK not in books or parse_time(event["commence_time"]) <= now:
        return {}
    fair = pinnacle_fair_prices(books[SHARP_BOOK])
    return {make_bet_id(event["id"], *key): (prob, price) for key, (prob, price, _) in fair.items()}


# ---------------------------------------------------------------------------
# Where the odds come from: the live API, or the demo file
# ---------------------------------------------------------------------------

class LiveOdds:
    """Downloads odds from The Odds API and keeps track of credits spent."""

    def __init__(self, api_key, max_credits):
        self.api_key = api_key
        self.max_credits = max_credits
        self.credits_used = 0
        self.credits_remaining = None

    def now(self):
        return datetime.now(timezone.utc)

    def _get(self, path, params, cost):
        """Make one API request, unless it would go over the credit budget."""
        if self.credits_used + cost > self.max_credits:
            print(f"  Skipped {path}: it would go over --max-credits {self.max_credits}")
            return None

        query = urllib.parse.urlencode(dict(params, apiKey=self.api_key))
        url = f"{API_BASE}{path}?{query}"
        try:
            with urllib.request.urlopen(url, timeout=30) as response:
                data = json.load(response)
                self.credits_used += int(response.headers.get("x-requests-last") or 0)
                self.credits_remaining = response.headers.get("x-requests-remaining")
        except urllib.error.HTTPError as error:
            if error.code == 401:
                sys.exit("The Odds API rejected your API key. Check ODDS_API_KEY.")
            print(f"  API error {error.code} on {path}: {error.read().decode()[:200]}")
            return None
        except urllib.error.URLError as error:
            print(f"  Could not reach The Odds API: {error.reason}")
            if "CERTIFICATE_VERIFY_FAILED" in str(error.reason):
                print('  Fix on Mac: double-click "Install Certificates.command" in your '
                      "Applications > Python 3.x folder, then run the scanner again.")
            return None
        return data

    def check(self):
        """Test the API key and connection with a free request. True if it worked."""
        return self._get("/sports", {}, cost=0) is not None

    def tennis_keys(self):
        """Active tennis tournaments. Listing sports costs no credits."""
        sports = self._get("/sports", {}, cost=0) or []
        return [s["key"] for s in sports
                if s["group"] == "Tennis" and s["active"] and not s["has_outrights"]]

    def game_odds(self, sport_key, markets):
        params = {
            "bookmakers": ",".join([SHARP_BOOK] + SOFT_BOOKS),
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
        }
        return self._get(f"/sports/{sport_key}/odds", params, cost=len(markets))

    def event_odds(self, sport_key, event_id, markets):
        params = {
            "bookmakers": ",".join([SHARP_BOOK] + SOFT_BOOKS),
            "markets": ",".join(markets),
            "oddsFormat": "decimal",
        }
        return self._get(f"/sports/{sport_key}/events/{event_id}/odds", params, cost=len(markets))

    def scores(self, sport_key):
        """Final and live scores from the last few days. Costs 2 credits."""
        return self._get(f"/sports/{sport_key}/scores", {"daysFrom": SCORES_DAYS_BACK}, cost=2)

    def summary(self):
        return (f"Credits used this run: {self.credits_used}. "
                f"Remaining this month: {self.credits_remaining}.")


class DemoOdds:
    """Serves made-up odds from demo_odds.json, in the same shape as the API."""

    def __init__(self, path=DEMO_FILE):
        with open(path) as file:
            self.data = json.load(file)

    def now(self):
        # The demo pretends it is always this moment, so its games never "start".
        return parse_time(self.data["demo_now"])

    @staticmethod
    def _only_markets(event, markets):
        event = copy.deepcopy(event)
        for book in event["bookmakers"]:
            book["markets"] = [m for m in book["markets"] if m["key"] in markets]
        return event

    def tennis_keys(self):
        return [key for key in self.data["game_odds"] if key.startswith("tennis_")]

    def game_odds(self, sport_key, markets):
        events = self.data["game_odds"].get(sport_key, [])
        return [self._only_markets(event, markets) for event in events]

    def event_odds(self, sport_key, event_id, markets):
        event = self.data["prop_odds"].get(event_id)
        return self._only_markets(event, markets) if event else None

    def summary(self):
        return "Demo mode: no credits used."


# ---------------------------------------------------------------------------
# Paper trading: log every edge as a pretend bet, then grade it
# ---------------------------------------------------------------------------

def load_paper_log(path=PAPER_LOG_FILE):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as file:
        return list(csv.DictReader(file))


def save_paper_log(rows, path=PAPER_LOG_FILE):
    with open(path, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=PAPER_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def log_paper_bets(edges, sharp_prices, now, path=PAPER_LOG_FILE):
    """
    Add new edges to the paper log and update closing prices for open bets.

    Each exact bet is logged once, at the first book and price it was seen
    at (edges are sorted best first, so that's the best book in that scan).

    Closing line value (CLV): every scan before a game starts records
    Pinnacle's latest fair price for each open bet. Whatever was seen last
    before the start counts as the "close". CLV = our odds x closing fair
    probability - 1, i.e. our EV measured against the closing line.
    Returns (bets added, open bets updated).
    """
    rows = load_paper_log(path)
    known = {row["bet_id"] for row in rows}
    bankroll = PAPER_BANKROLL + sum(float(row["kelly_profit"]) for row in rows if row["kelly_profit"])

    added = 0
    for edge in edges:
        if edge["bet_id"] in known:
            continue
        known.add(edge["bet_id"])
        rows.append({
            "bet_id": edge["bet_id"],
            "logged_at": now.isoformat(timespec="minutes"),
            "sport": edge["sport"],
            "sport_key": edge["sport_key"],
            "event_id": edge["event_id"],
            "game": edge["game"],
            "starts": edge["starts"].isoformat(),
            "market_key": edge["market_key"],
            "market": edge["market"],
            "outcome": edge["outcome"],
            "player": edge["player"] or "",
            "point": "" if edge["point"] is None else edge["point"],
            "bet": edge["bet"],
            "book": edge["book"],
            "odds": edge["odds"],
            "decimal_odds": edge["decimal_odds"],
            "fair_prob": round(edge["fair_prob"], 4),
            "ev_pct": round(edge["ev_pct"], 2),
            "stake_pct": round(edge["stake_pct"], 2),
            "kelly_stake": round(bankroll * edge["stake_pct"] / 100, 2),
            "status": "open",
        })
        added += 1

    updated = record_closes(rows, sharp_prices, now)
    save_paper_log(rows, path)
    return added, updated


def record_closes(rows, sharp_prices, now):
    """Store Pinnacle's latest fair price on open bets in games not yet started."""
    updated = 0
    for row in rows:
        if row["status"] != "open" or row["bet_id"] not in sharp_prices:
            continue
        if parse_time(row["starts"]) <= now:
            continue  # the close is frozen once the game starts
        fair_prob, pinnacle_price = sharp_prices[row["bet_id"]]
        row["close_fair_prob"] = round(fair_prob, 4)
        row["close_pinnacle_odds"] = decimal_to_american(pinnacle_price)
        row["close_seen_at"] = now.isoformat(timespec="minutes")
        row["clv_pct"] = round(expected_value(fair_prob, float(row["decimal_odds"])) * 100, 2)
        updated += 1
    return updated


def capture_closes(source, path=PAPER_LOG_FILE):
    """
    Record closing prices for open paper bets whose games start very soon.

    Meant to run automatically every 10 minutes (see .github/workflows). Only
    games starting within CLOSE_WINDOW_MINUTES are fetched, and only the
    markets we have bets in, so most runs use no credits at all.
    """
    now = source.now()
    window = timedelta(minutes=CLOSE_WINDOW_MINUTES)
    refresh = timedelta(minutes=CLOSE_REFRESH_MINUTES)
    rows = load_paper_log(path)

    due = {}  # (sport key, event ID) -> markets we hold bets in
    for row in rows:
        if row["status"] != "open":
            continue
        starts = parse_time(row["starts"])
        if not now < starts <= now + window:
            continue
        if row["close_seen_at"] and parse_time(row["close_seen_at"]) > now - refresh:
            continue  # price was recorded moments ago
        due.setdefault((row["sport_key"], row["event_id"]), set()).add(row["market_key"])

    if not due:
        return  # nothing starting soon: stay quiet, spend nothing

    sharp_prices = {}
    for (sport_key, event_id), markets in due.items():
        event = source.event_odds(sport_key, event_id, sorted(markets))
        if event:
            sharp_prices.update(current_sharp_prices(event, now))

    updated = record_closes(rows, sharp_prices, now)
    save_paper_log(rows, path)
    print(f"{now:%Y-%m-%d %H:%M} UTC: recorded closing prices for {updated} bet(s) "
          f"in {len(due)} game(s). {source.summary()}")
    return updated


def grade_bet(row, scores):
    """
    Grade one bet from the final score ({team or player: score}).
    Returns "won", "lost" or "push", or None for markets we can't grade.
    Tennis scores are sets won, which is enough for moneylines.
    """
    market = row["market_key"]
    point = float(row["point"]) if row["point"] else 0.0

    if market in ("h2h", "spreads"):
        mine = scores[row["outcome"]]
        theirs = max(score for name, score in scores.items() if name != row["outcome"])
        margin = mine + point - theirs
    elif market == "totals":
        total = sum(scores.values())
        margin = total - point if row["outcome"] == "Over" else point - total
    else:
        return None

    if margin > 0:
        return "won"
    if margin < 0:
        return "lost"
    return "push"


def bet_profit(status, decimal_odds, stake):
    if status == "won":
        return stake * (decimal_odds - 1)
    if status == "lost":
        return -stake
    return 0.0


def settle_paper_bets(source, path=PAPER_LOG_FILE):
    """Download final scores and grade every open paper bet whose game has finished."""
    now = source.now()
    rows = load_paper_log(path)
    started = [row for row in rows
               if row["status"] == "open" and row["market_key"] in SETTLEABLE_MARKETS
               and parse_time(row["starts"]) <= now]
    if not started:
        print("No open paper bets have started yet, so there's nothing to settle.")
        return

    # One scores request per sport (2 credits each), not one per bet.
    final_scores = {}
    for sport_key in sorted({row["sport_key"] for row in started}):
        for event in source.scores(sport_key) or []:
            if event["completed"] and event["scores"]:
                final_scores[event["id"]] = {s["name"]: float(s["score"]) for s in event["scores"]}

    settled = 0
    for row in started:
        scores = final_scores.get(row["event_id"])
        if scores is None:
            continue  # not finished yet
        status = grade_bet(row, scores)
        decimal_odds = float(row["decimal_odds"])
        row["status"] = status
        row["final_score"] = ", ".join(f"{name} {score:g}" for name, score in scores.items())
        row["flat_profit"] = round(bet_profit(status, decimal_odds, 1.0), 4)
        row["kelly_profit"] = round(bet_profit(status, decimal_odds, float(row["kelly_stake"])), 2)
        settled += 1
    save_paper_log(rows, path)

    print(f"Settled {settled} paper bet(s). {len(started) - settled} started but not finished yet.")
    too_old = [row for row in started if row["status"] == "open"
               and parse_time(row["starts"]) < now - timedelta(days=SCORES_DAYS_BACK)]
    if too_old:
        print(f"Warning: {len(too_old)} bet(s) started more than {SCORES_DAYS_BACK} days ago and "
              f"can't be settled automatically anymore. Set their status to void in paper_bets.csv.")


def average(values):
    values = [float(value) for value in values]
    return sum(values) / len(values)


def paper_report(path=PAPER_LOG_FILE):
    """Print how the paper bets are doing: CLV first, then win/loss."""
    rows = load_paper_log(path)
    if not rows:
        print("No paper bets yet. Every real scan logs the edges it finds automatically.")
        return

    settled = [row for row in rows if row["status"] in ("won", "lost", "push")]
    graded = [row for row in settled if row["status"] != "push"]
    # Only count CLV when the close was seen AFTER the bet was logged.
    # Otherwise the "close" is just the price at the time, and CLV equals EV.
    with_close = [row for row in rows if row["clv_pct"]
                  and parse_time(row["close_seen_at"]) > parse_time(row["logged_at"])]

    print(f"Paper bets: {len(rows)} logged, {len(settled)} settled, {len(rows) - len(settled)} open")
    print(f"Average EV when logged: {average(row['ev_pct'] for row in rows):+.2f}%")

    if with_close:
        beat = sum(1 for row in with_close if float(row["clv_pct"]) > 0)
        print(f"\nClosing line value ({len(with_close)} bets with a later closing price)")
        print(f"  Average CLV:            {average(row['clv_pct'] for row in with_close):+.2f}%")
        print(f"  Beat the closing line:  {beat / len(with_close):.0%} of bets")

    if graded:
        wins = sum(1 for row in settled if row["status"] == "won")
        pushes = len(settled) - len(graded)
        flat_units = sum(float(row["flat_profit"]) for row in settled)

        # What the EVs said we should have won, and how much luck can move that.
        # A 1-unit bet at decimal odds d with win chance p has variance p(1-p)d^2.
        expected_units = sum(expected_value(float(row["fair_prob"]), float(row["decimal_odds"]))
                             for row in graded)
        std_dev = math.sqrt(sum(float(row["fair_prob"]) * (1 - float(row["fair_prob"]))
                                * float(row["decimal_odds"]) ** 2 for row in graded))

        kelly_staked = sum(float(row["kelly_stake"]) for row in graded)
        kelly_profit = sum(float(row["kelly_profit"]) for row in settled)

        print(f"\nResults: {wins}-{len(graded) - wins}-{pushes} (won-lost-push)")
        print(f"  Flat 1 unit per bet:    {flat_units:+.2f} units, ROI {flat_units / len(graded):+.1%}")
        print(f"  Expected from EV:       {expected_units:+.2f} units")
        print(f"  Luck check:             {(flat_units - expected_units) / std_dev:+.1f} standard deviations "
              f"from expected (between -2 and +2 is normal luck)")
        print(f"  Quarter-Kelly stakes:   ${kelly_profit:+,.2f} on ${kelly_staked:,.2f} staked, "
              f"ROI {kelly_profit / kelly_staked:+.1%}")
        print(f"  Paper bankroll:         ${PAPER_BANKROLL + kelly_profit:,.2f} "
              f"(started at ${PAPER_BANKROLL:,.0f})")

    print("\nCLV starts to mean something after about 50 bets. Win/loss needs several hundred.")


# ---------------------------------------------------------------------------
# Dashboard: an interactive HTML page of current bets and paper results
# ---------------------------------------------------------------------------

def save_latest_scan(edges, scanned_at, demo, min_ev, path=LATEST_SCAN_FILE):
    """Save the latest scan's bets for the dashboard's "Bets to take" tab."""
    keep = ["bet_id", "ev_pct", "stake_pct", "sport", "game", "market", "bet", "book",
            "odds", "fair_odds", "pinnacle_odds", "decimal_odds", "fair_prob"]
    scan = {
        "scanned_at": scanned_at.isoformat(),
        "demo": demo,
        "min_ev": min_ev,
        "edges": [dict({key: edge[key] for key in keep}, starts=edge["starts"].isoformat())
                  for edge in edges],
    }
    with open(path, "w") as file:
        json.dump(scan, file, indent=1)


def build_dashboard(default_tab="bets", scan_path=LATEST_SCAN_FILE,
                    log_path=PAPER_LOG_FILE, out_path=DASHBOARD_FILE):
    """
    Fill dashboard_template.html with the latest scan and the paper log, and
    save it as dashboard.html. The page does all its charts and stats in the
    browser, so it works offline and needs nothing installed.
    """
    latest_scan = None
    if os.path.exists(scan_path):
        with open(scan_path) as file:
            latest_scan = json.load(file)

    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "default_tab": default_tab,
        "paper_bankroll": PAPER_BANKROLL,
        "latest_scan": latest_scan,
        "paper_bets": load_paper_log(log_path),
    }
    with open(DASHBOARD_TEMPLATE) as file:
        template = file.read()
    # "</" is escaped so nothing in the data can end the <script> tag early.
    page = template.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
    with open(out_path, "w") as file:
        file.write(page)
    return out_path


def open_dashboard(default_tab):
    path = build_dashboard(default_tab)
    webbrowser.open(Path(path).as_uri())
    print(f"Opened {os.path.basename(path)} in your browser.")


# ---------------------------------------------------------------------------
# Running a scan
# ---------------------------------------------------------------------------

def run_scan(source, sport_names, markets, include_props, max_prop_games, min_ev):
    """
    Scan every requested sport and return all edges, best first.

    markets is the list of game markets to scan for every sport, or None to
    use each sport's own default (SPORT_MARKETS, otherwise GAME_MARKETS).

    Also returns Pinnacle's current fair prices ({bet ID: (fair prob, price)}),
    which the paper log uses to track closing line value.
    """
    now = source.now()
    edges = []
    sharp_prices = {}

    for name in sport_names:
        sport_markets = markets or SPORT_MARKETS.get(name, GAME_MARKETS).split(",")
        sport_keys = source.tennis_keys() if name == "tennis" else [SPORTS[name]]
        for sport_key in sport_keys:
            events = source.game_odds(sport_key, sport_markets) or []
            print(f"  {sport_key}: {len(events)} games")
            for event in events:
                edges += find_edges(event, name.upper(), now, min_ev)
                sharp_prices.update(current_sharp_prices(event, now))

            if include_props and name in PROP_MARKETS:
                # Props cost credits per game, so only check the next few games.
                upcoming = sorted(
                    (e for e in events if parse_time(e["commence_time"]) > now),
                    key=lambda e: e["commence_time"],
                )
                for event in upcoming[:max_prop_games]:
                    prop_event = source.event_odds(sport_key, event["id"], PROP_MARKETS[name])
                    if prop_event:
                        edges += find_edges(prop_event, name.upper(), now, min_ev)
                        sharp_prices.update(current_sharp_prices(prop_event, now))

    edges.sort(key=lambda edge: edge["ev_pct"], reverse=True)
    return edges, sharp_prices


def stake_text(edge, bankroll):
    text = f"{edge['stake_pct']:.1f}%"
    if bankroll:
        text += f" (${bankroll * edge['stake_pct'] / 100:,.0f})"
    return text


def print_edges(edges, bankroll):
    """Print the edges as a lined-up table."""
    headers = ["EV", "Stake", "Book", "Odds", "Fair", "Pinn", "Sport", "Market", "Bet", "Game", "Starts"]
    rows = [[
        f"{edge['ev_pct']:+.1f}%",
        stake_text(edge, bankroll),
        edge["book"],
        format_american(edge["odds"]),
        format_american(edge["fair_odds"]),
        format_american(edge["pinnacle_odds"]),
        edge["sport"],
        edge["market"],
        edge["bet"],
        edge["game"],
        edge["starts"].astimezone().strftime("%a %b %d %I:%M %p"),
    ] for edge in edges]

    widths = [max(len(row[i]) for row in rows + [headers]) for i in range(len(headers))]
    line = "  ".join(header.ljust(width) for header, width in zip(headers, widths))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)))


def save_csv(edges, bankroll):
    """Save the edges to edges_YYYYMMDD_HHMMSS.csv and return the file name."""
    filename = os.path.join(SCRIPT_DIR, datetime.now().strftime("edges_%Y%m%d_%H%M%S.csv"))
    columns = ["ev_pct", "stake_pct", "stake_dollars", "sport", "game", "starts", "market",
               "bet", "book", "odds", "fair_odds", "pinnacle_odds", "decimal_odds", "fair_prob"]
    with open(filename, "w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for edge in edges:
            row = dict(edge)
            row["ev_pct"] = round(edge["ev_pct"], 2)
            row["stake_pct"] = round(edge["stake_pct"], 2)
            row["stake_dollars"] = round(bankroll * edge["stake_pct"] / 100, 2) if bankroll else ""
            row["starts"] = edge["starts"].astimezone().isoformat(timespec="minutes")
            row["fair_prob"] = round(edge["fair_prob"], 4)
            writer.writerow(row)
    return filename


def parse_args():
    parser = argparse.ArgumentParser(
        description="Find +EV bets by comparing Ohio sportsbooks to Pinnacle's no-vig line.")
    parser.add_argument("--demo", action="store_true",
                        help="use the made-up odds in demo_odds.json (costs no credits)")
    parser.add_argument("--check", action="store_true",
                        help="test your API key and connection, then stop (costs no credits)")
    parser.add_argument("--sports", default=DEFAULT_SPORTS,
                        help=f"comma-separated, any of: {','.join(SPORTS)} (default: {DEFAULT_SPORTS})")
    parser.add_argument("--markets",
                        help=f"game markets to scan for every sport (default: {GAME_MARKETS}, "
                             "tennis moneyline only)")
    parser.add_argument("--props", action="store_true",
                        help="also scan player props (NFL and NBA; costs extra credits)")
    parser.add_argument("--max-prop-games", type=int, default=DEFAULT_MAX_PROP_GAMES,
                        help=f"games per sport to check props for (default: {DEFAULT_MAX_PROP_GAMES})")
    parser.add_argument("--min-ev", type=float, default=DEFAULT_MIN_EV,
                        help=f"minimum EV in percent (default: {DEFAULT_MIN_EV})")
    parser.add_argument("--bankroll", type=float,
                        help="your bankroll in dollars, to show stakes in $")
    parser.add_argument("--max-credits", type=int, default=DEFAULT_MAX_CREDITS,
                        help=f"most API credits this run may spend (default: {DEFAULT_MAX_CREDITS})")
    parser.add_argument("--close", action="store_true",
                        help="record closing prices for paper bets starting soon (run by the scheduler)")
    parser.add_argument("--settle", action="store_true",
                        help="grade finished paper bets using final scores (2 credits per sport)")
    parser.add_argument("--report", action="store_true",
                        help="show how the paper bets are doing and open the dashboard (costs no credits)")
    parser.add_argument("--dashboard", action="store_true",
                        help="open the dashboard on the latest scan's bets (costs no credits)")
    parser.add_argument("--no-open", action="store_true",
                        help="don't open the dashboard in your browser after a scan")
    return parser.parse_args()


def main():
    args = parse_args()

    if args.report:
        paper_report()
        open_dashboard("perf")
        return

    if args.dashboard:
        open_dashboard("bets")
        return

    sport_names =[s.strip().lower() for s in args.sports.split(",") if s.strip()]
    unknown = [s for s in sport_names if s not in SPORTS]
    if unknown:
        sys.exit(f"Unknown sport(s): {', '.join(unknown)}. Choose from: {', '.join(SPORTS)}")
    # None means "use each sport's default markets".
    markets = [m.strip() for m in args.markets.split(",") if m.strip()] if args.markets else None

    if args.demo:
        if args.settle or args.close:
            sys.exit("--settle and --close work on real paper bets, so they can't be combined with --demo.")
        source = DemoOdds()
        print("DEMO MODE: made-up odds, not real prices.\n")
    else:
        api_key = os.environ.get("ODDS_API_KEY") or API_KEY
        if not api_key:
            sys.exit("No API key found. Set the ODDS_API_KEY environment variable (see README.md).")
        source = LiveOdds(api_key, args.max_credits)

        if args.check:
            if source.check():
                print(f"Connection and API key OK. Credits remaining this month: "
                      f"{source.credits_remaining}")
            return

        if args.settle:
            settle_paper_bets(source)
            build_dashboard()
            print(source.summary())
            return

        if args.close:
            if capture_closes(source):
                build_dashboard()
            return

    print(f"Scanning {', '.join(sport_names)} for bets at or above {args.min_ev:g}% EV...")
    edges, sharp_prices = run_scan(source, sport_names, markets, args.props,
                                   args.max_prop_games, args.min_ev)
    print()

    if edges:
        print(f"Found {len(edges)} bet(s) at or above {args.min_ev:g}% EV:\n")
        print_edges(edges, args.bankroll)
        print(f"\nSaved to {os.path.basename(save_csv(edges, args.bankroll))}")
    else:
        print("No bets found at or above the minimum EV.")

    # Real scans feed the paper test. Demo odds are made up, so they don't.
    if not args.demo:
        added, updated = log_paper_bets(edges, sharp_prices, source.now())
        print(f"Paper log: {added} new bet(s) added, closing prices updated for {updated} open bet(s).")

    save_latest_scan(edges, datetime.now(timezone.utc), args.demo, args.min_ev)
    if args.no_open:
        build_dashboard()
        print("Dashboard updated.")
    else:
        open_dashboard("bets")

    print(source.summary())
    print(f"\n{RECHECK_REMINDER}")


if __name__ == "__main__":
    main()
