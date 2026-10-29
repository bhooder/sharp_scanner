# Sharp Line Scanner — project brief for Claude

Private notes about the owner and their setup live in `CLAUDE.local.md`, which is git-ignored and never uploaded. Keep this file neutral: project facts only, no personal details.

## What this project does
Finds positive expected value (+EV) sports bets by comparing Ohio sportsbook prices to Pinnacle's line. Keep the code clean, commented and readable: it's a public portfolio project.

1. Download current odds from The Odds API (the-odds-api.com, v4) for NFL, college football, NBA and tennis.
2. Treat Pinnacle as the sharp "true" price. Remove its vig with Shin's method (`remove_vig`). For two-sided bets this equals the additive method: each side gives up an equal number of percentage points. Chosen over multiplicative (Oct 27, 2026) because multiplicative overrates underdogs (favorite-longshot bias): on a real bet, Kings +1040, multiplicative said +7.6% EV and Shin said −7.3%.
3. Compare every soft book's price on the exact same bet (same market, outcome name, player and point/line).
4. EV = fair_probability × decimal_odds − 1. Flag anything at or above the minimum EV (in percent, default 2%), sort best first, print it, and save a timestamped CSV.
5. Suggest a stake: quarter Kelly (EV ÷ (decimal odds − 1) × 0.25), capped at 2.5% of bankroll.

Worked example: Pinnacle Over −120 / Under +100 → implied 54.5% + 50.0% = 104.5% → each side gives up 2.27 points → fair Over ≈ 52.3% (≈ −110). A soft book offering the Over at +105 → EV ≈ +7.2%.

## Files
- `sharp_line_scanner.py` — the whole program. Standard library only (urllib, json, csv, argparse), so nothing to pip install. Keep it that way unless there's a clear reason.
- `test_sharp_line_scanner.py` — unit tests (`python3 -m unittest -v`).
- `demo_odds.json` — made-up odds in The Odds API's response shape, used by `--demo`. It has a fixed `demo_now` time, and each entry exercises a rule (edge, different line, stale price, started game, props).
- `README.md` — setup, usage and a write-up of the math.
- `.github/workflows/scanner.yml` — runs the scanner on GitHub Actions (see below).
- `dashboard_template.html` — the dashboard page (HTML/CSS/vanilla JS with hand-drawn SVG charts, no libraries, works offline). Python swaps its `__DATA__` placeholder for JSON (latest scan + paper log) and writes `dashboard.html`. All stats and charts are computed in the browser so the filters can recompute them. Colors follow the dataviz skill's reference palette, with light and dark modes.
- Generated and git-ignored: `paper_bets.csv` (paper log, real data: never overwrite or delete without asking), `latest_scan.json`, `dashboard.html`, `edges_*.csv`.

## Running on GitHub Actions
- One workflow, `scanner.yml`:
  - **scan**: manual only ("Run workflow", also from the GitHub phone app), with inputs for sports and minimum EV.
  - **close**: cron every 10 minutes.
  - **settle**: cron daily at 13:00 UTC.
- The API key is the `ODDS_API_KEY` repository secret.
- Data persists on the orphan `data` branch: `paper_bets.csv`, `latest_scan.json`, `dashboard.html`, `scans/edges_*.csv`. Each run copies these into the workspace, runs, copies them back and commits only if something changed. The `main` branch holds code only, so the bot never commits to it.
- When data changes, the dashboard is published to GitHub Pages (Pages source: GitHub Actions).
- `concurrency: scanner-data` keeps runs from overlapping.
- GitHub's cron often starts late, so `--close` uses a 30-minute window and refreshes the price on each run, skipping a game if its price was recorded within the last 5 minutes. The last run before the start sets the close, which costs about 1 credit per game per run.

## Paper testing
- Every real (non-demo) scan logs every edge to `paper_bets.csv` once (first sighting, best book), and updates Pinnacle's latest fair price for open bets whose games haven't started. The last price seen before the start is the "close". CLV = odds × closing fair prob − 1.
- `--close` fetches Pinnacle prices (per-event endpoint) only for games with open paper bets starting within `CLOSE_WINDOW_MINUTES`.
- The report counts CLV only when `close_seen_at` is later than `logged_at`.
- `--settle` grades finished h2h/spreads/totals bets from `/v4/sports/{sport}/scores?daysFrom=3` (2 credits per sport with started bets). Tennis scores are sets won (verified Oct 2026).
- `--report` (free): CLV, win/loss, flat units vs expected units with a standard-deviation luck check, and quarter-Kelly paper bankroll (starts at $1,000).

## How to run locally
```
python3 sharp_line_scanner.py --demo --props
python3 sharp_line_scanner.py
python3 sharp_line_scanner.py --sports nfl,nba --props --bankroll 500 --min-ev 3
```
The API key is read from the `ODDS_API_KEY` environment variable. `API_KEY` in the script stays blank. Never put the real key in project files.

## Current focus
- Default scan: NBA (moneyline, spreads, totals) and tennis (moneyline only, via `SPORT_MARKETS`). About 6 credits per scan with 3 tennis tournaments active.
- Tennis covers whatever tournaments The Odds API lists as active: ATP and WTA Slams, 1000s and some 500s. No Challengers, ITF or most 250s.
- No player props for now. The `--props` code stays but is off unless the flag is given. NFL and CFB are available with `--sports nfl,cfb`.

## Rules the scanner follows
- Exact-line matches only.
- Skip a comparison if the soft book's price was updated more than 5 minutes after Pinnacle's (Pinnacle may be stale).
- Skip games that already started.
- Skip one-sided Pinnacle prices (can't remove vig).
- No college player props (illegal in Ohio). The API has no tennis props.
- Output shows American odds.

## The Odds API notes
- Game lines: `GET /v4/sports/{sport}/odds`. Props: `GET /v4/sports/{sport}/events/{eventId}/odds`. Both with `bookmakers`, `markets`, `oddsFormat=decimal`, `apiKey`.
- `GET /v4/sports` is free and is used to find active tennis tournaments (each tournament is its own sport key).
- Cost = markets × regions. Pinnacle + up to 9 books in `bookmakers` = 1 region. Current list is Pinnacle + 8 Ohio books, so 1 credit per market per request. Props cost per game.
- Free plan: 500 credits/month. `--max-credits` (default 50) caps each run. Headers `x-requests-last` / `x-requests-remaining` show usage.
- Never print the request URL: it contains the API key, and Actions logs are public.
- Verified Oct 2026: Pinnacle has NFL and NBA player props on the API. `williamhill_us` (Caesars) and `fanatics` didn't appear in a test pull, so check whether they ever show up. Ohio has its own Hard Rock key, `hardrockbet_oh`.

## Ideas for next steps (in rough order)
1. A real bet log for bets actually placed (paper testing already covers CLV and P/L for all edges).
2. Optional automatic scans on a schedule, with a credit budget.
3. Fair-odds comparison for lines that differ by half a point.
4. Optionally log the multiplicative and power EVs alongside Shin, and measure which best predicts the closing line.
5. GitHub polish: screenshots of the dashboard.

## Ground rules
- This tool only finds and reports prices. It should never place bets or log into sportsbook accounts.
- Prices change fast. Output should keep reminding the user to recheck the price in the app before betting.
