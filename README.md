# Sharp Line Scanner

Finds positive expected value (+EV) sports bets by comparing Ohio sportsbook
prices to Pinnacle, the sharpest book in the market.

The tool only finds and reports prices. It never places bets or logs into
sportsbook accounts.

## The idea

Pinnacle takes big bets from professional bettors and moves its prices fast,
so its line is the best public estimate of each outcome's true probability.
Softer books (DraftKings, FanDuel, ...) are sometimes slower to move. When a
soft book pays more than the true probability justifies, the bet has positive
expected value.

## The math

**1. Remove Pinnacle's vig.** A book's prices add up to more than 100%. That
extra is the vig, the book's built-in margin. The scanner removes it with
**Shin's method**. For a two-sided bet that is the same as the additive
method: each side gives up an equal number of percentage points.

| Pinnacle | Odds | Implied probability | Fair probability |
|---|---|---|---|
| Over 47.5 | −120 | 120 / 220 = 54.5% | 54.5 − 2.27 = **52.3%** (≈ −110) |
| Under 47.5 | +100 | 100 / 200 = 50.0% | 50.0 − 2.27 = 47.7% |
| Total | | 104.5% | 100% |

**Why not just scale every side down by the same percentage** (the
"multiplicative" method)? Bookmakers hide more of their margin in underdog
prices (the *favorite-longshot bias*). Scaling proportionally overrates
underdogs, so longshots look like edges when they aren't. With Pinnacle's
usual ~3% margin, the multiplicative method adds this much fake EV to a bet on
the underdog, compared with Shin:

| Underdog | +150 | +200 | +300 | +600 | +900 |
|---|---|---|---|---|---|
| Extra EV from multiplicative | 0.8 pts | 1.6 pts | 3.1 pts | 7.6 pts | 12.1 pts |

On a real NBA bet (Kings +1040 at FanDuel, Pinnacle +927 / −1430), the
multiplicative method said **+7.6% EV**, while Shin says **−7.3%**.

Shin's model (H. S. Shin, 1993) assumes a share *z* of the money comes from
bettors with inside information, and that the bookmaker pads longshots to
protect against them. The scanner finds the *z* that makes the probabilities
add to 100%. Unlike the plain additive method, it can never give a negative
probability, and it still works for bets with three or more outcomes.

**2. Expected value.** If DraftKings offers the Over at +105 (decimal 2.05):

EV = fair probability × decimal odds − 1 = 0.523 × 2.05 − 1 ≈ **+7.2%**

That means about 7 cents of expected profit per dollar bet over the long run.

**3. Suggested stake.** The [Kelly criterion](https://en.wikipedia.org/wiki/Kelly_criterion)
gives the bankroll fraction that maximises long-run growth:
EV ÷ (decimal odds − 1). Full Kelly swings a lot and assumes the fair
probability is exactly right, so the scanner suggests **a quarter of Kelly,
capped at 2.5% of bankroll**. Bigger edges get bigger stakes, and long
shots get smaller ones.

## Rules the scanner follows

- It only compares **the exact same bet**: same market, outcome, player and line.
  A soft book's −3 is never compared to Pinnacle's −2.5.
- It skips a comparison if the soft book's price was updated **more than 5 minutes
  after Pinnacle's**, because Pinnacle's price may be out of date.
- It skips games that have already started.
- It skips one-sided prices (for example "anytime TD: Yes" only), since the vig
  can't be removed without both sides.
- No college player props: Ohio doesn't allow them.

## Setup (Mac)

1. **Python 3** — check with `python3 --version`. The script uses only Python's
   standard library, so there's nothing to install.
2. **Certificates (one time).** If you installed Python from python.org, open
   Finder → Applications → Python 3.x and double-click
   `Install Certificates.command`. Without this, downloads fail with a
   `CERTIFICATE_VERIFY_FAILED` error.
3. **API key.** Get a free key at [the-odds-api.com](https://the-odds-api.com),
   then save it in your shell settings (replace `your-key-here`):

   ```
   echo 'export ODDS_API_KEY="your-key-here"' >> ~/.zshrc
   source ~/.zshrc
   ```

   Never paste your key into the code or share it.

## Usage

```
python3 sharp_line_scanner.py --demo                  # made-up odds, costs nothing
python3 sharp_line_scanner.py --check                 # test key and connection, costs nothing
python3 sharp_line_scanner.py                         # NBA and tennis game lines
python3 sharp_line_scanner.py --props                 # also NFL and NBA player props
python3 sharp_line_scanner.py --sports nba --bankroll 500 --min-ev 3
```

| Option | Meaning | Default |
|---|---|---|
| `--sports` | any of `nfl,cfb,nba,tennis` | `nba,tennis` |
| `--markets` | game markets to scan for every sport | `h2h,spreads,totals`; tennis `h2h` only |
| `--props` | also scan player props (NFL, NBA) | off |
| `--max-prop-games` | games per sport to check props for | 3 |
| `--min-ev` | minimum EV in **percent** | 2 |
| `--bankroll` | show stakes in dollars as well as percent | none |
| `--max-credits` | most API credits one run may spend | 50 |
| `--close` | record closing prices for games starting soon | |
| `--settle` | grade finished paper bets | |
| `--report` | show paper testing results and open the dashboard | |
| `--dashboard` | open the dashboard on the latest scan's bets | |
| `--no-open` | don't open the dashboard after a scan | |

Results print as a table, best first, and are saved to `edges_YYYYMMDD_HHMMSS.csv`.

In the table, **Odds** is the soft book's price, **Fair** is Pinnacle's no-vig
price, and **Pinn** is Pinnacle's actual price on the same side.

## Paper testing

Paper testing checks whether the edges are real before any money is risked.

**Every real scan logs every edge it finds** to `paper_bets.csv` as a pretend
bet. Each exact bet is logged once, at the first (best) price seen, so there's
no cherry-picking. Demo scans are never logged.

Each bet is judged two ways:

1. **Closing line value (CLV).** Each scan before a game starts also records
   Pinnacle's latest fair price for every open paper bet. The last one seen
   before the start counts as the close. CLV = our odds × closing fair
   probability − 1, which is our EV measured against the closing line. If the
   scanner is finding real edges, average CLV should be positive. CLV gives a
   useful signal after about 50 bets, while win/loss is mostly luck for the first
   few hundred. **The close is only as good as your last scan before the game**,
   so scanning close to game time makes it more accurate.
2. **Win/loss.** `--settle` downloads final scores and grades finished bets.
   Profit is tracked two ways: flat 1 unit per bet, and quarter-Kelly stakes
   from a $1,000 paper bankroll.

```
python3 sharp_line_scanner.py            # scan and log edges (about 6 credits)
python3 sharp_line_scanner.py --settle   # grade finished bets (2 credits per sport)
python3 sharp_line_scanner.py --report   # see how it's going (free)
```

### Automatic closing prices

`python3 sharp_line_scanner.py --close` records Pinnacle's price for open paper
bets whose games start within the next 30 minutes, fetching only those games and
only the markets with bets in them (about 1 credit per game). If nothing is about
to start, it does nothing and costs nothing. On GitHub it runs every 10 minutes
(see below), and the last run before each game starts sets the close.

The report only counts CLV for bets whose close was recorded *after* they were
logged. A bet found minutes before the start has no later price, so its "CLV"
would just repeat its EV.

Run `--settle` about once a day. The API only returns scores from the last
3 days, so older bets can't be graded automatically.

The report also includes a **luck check**: actual flat profit minus the profit
the EVs predicted, divided by the standard deviation. A 1-unit bet at decimal
odds *d* with win probability *p* has variance *p(1 − p)d²*. Anything between
−2 and +2 is normal luck. A result well below −2 suggests the edges aren't real.

Known limits: if Pinnacle moves its line (say a total from 221.5 to 222.5),
the scanner can't see the close for the old line, so the last price seen before
the move stands in for it. Tennis retirements are graded on the reported score,
which may not match a book's retirement rules.

## Dashboard

Every scan, settle and recorded close rebuilds `dashboard.html`, a single
interactive page that works offline. **Each scan also opens it in your browser
automatically** (add `--no-open` to skip that). You can also double-click
`dashboard.html` in Finder, or open it with:

```
python3 sharp_line_scanner.py --dashboard   # open on "Bets to take"
python3 sharp_line_scanner.py --report      # print the summary and open on "Paper performance"
```

- **Bets to take:** the latest scan's bets, best first, with suggested stakes
  for the bankroll you type in. Games that have started are hidden, and it warns
  you when the scan is more than 15 minutes old.
- **Paper performance:** filters for date, sport, market and book, plus a
  flat-units / quarter-Kelly toggle. Every number below the filters updates to
  match:
  - headline average CLV and how often bets beat the close
  - record, profit, ROI and the luck check
  - a cumulative profit chart, actual vs expected, with the ±2 SD luck band
  - a histogram of CLV per bet
  - a breakdown by sport, book, market or time before start
  - a sortable table of every paper bet

The page is built from `dashboard_template.html`. Edit that file to change the
look, then run any command above to rebuild it.

## Running on GitHub (no computer needed)

`.github/workflows/scanner.yml` runs everything on GitHub Actions, which is free
for public repositories:

| Task | When | Credits |
|---|---|---|
| **scan** | by hand: Actions tab → Scanner → Run workflow (works in the GitHub phone app) | about 6 |
| **close** | every 10 minutes | 0, or about 1 per game starting soon |
| **settle** | daily at 13:00 UTC | 2 per sport with finished bets |

The paper log and scan results are saved on the repository's `data` branch, so
the `main` branch only ever holds code. After any run that changes something,
the dashboard is published to GitHub Pages at
`https://<your-username>.github.io/<repo-name>/`.

One-time setup for your own copy:

1. **Settings → Secrets and variables → Actions → New repository secret.**
   Name it `ODDS_API_KEY` and paste your key as the value. GitHub keeps it
   hidden, even in the public run logs.
2. **Settings → Pages → Source: GitHub Actions.**
3. **Actions tab** → enable workflows if asked → **Scanner → Run workflow.**

GitHub's timer is not exact: scheduled runs often start 5–30 minutes late, so
some closing prices are taken up to about half an hour before the start.

## API credits

The free plan has 500 credits a month. Each request costs 1 credit per market:

| Scan | Credits |
|---|---|
| One sport, 3 game markets | 3 |
| Tennis, moneyline only | 1 per active tournament |
| Player props | 4 per game (3 games per sport by default) |
| Default scan (NBA, tennis) | 3 + 1 per tennis tournament (about 6 with 3 tournaments) |
| With `--props` | up to 12 more for NBA (CFB props aren't scanned) |

The scanner stops before going over `--max-credits` and prints what it used
and what's left this month.

## Tests

```
python3 -m unittest -v
```

The tests check the odds math against the worked example above, the
bet-matching rules and the demo scan.

## Ohio sportsbooks scanned

DraftKings, FanDuel, BetMGM, Caesars, ESPN BET, Fanatics, BetRivers and Hard Rock
Bet (Ohio). bet365 is legal in Ohio but not offered by The Odds API. Books
get renamed and enter or leave states, so check the
[Ohio Casino Control Commission](https://casinocontrol.ohio.gov) list now and then
and update `SOFT_BOOKS` in the script.

---

## License

MIT. See [LICENSE](LICENSE).

---

*Prices move fast. Always recheck the price in the sportsbook app before betting.*
