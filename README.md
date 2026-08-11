# Stage 2 Trend Template Screener — NSE Mid/Small Cap

Long-only screener for stocks in a confirmed Stage 2 uptrend (Minervini's
8-point Trend Template) with liquidity filters, a VCP contraction/volume-dryup
overlay for entry timing, and a leak-free walk-forward backtester. Pure
Python, no numpy/pandas in the analysis logic.

## Files

| File | Purpose |
|---|---|
| `screener_core.py` | Shared trend-template/liquidity/RS logic. Every function takes an `end_index` so it can evaluate "as of" any historical bar — this is what makes the backtest leak-free. Used by both the live screener and the backtester. |
| `vcp_detector.py` | VCP overlay: contracting-swing detection (zigzag pivots), volume dry-up ratio, ATR range tightness. Runs only on names that already pass Stage 2. |
| `episodic_pivot_scanner.py` | Finds "episodic pivot" / power-play setups: a single-day range-expansion move on huge volume, followed by an orderly hold near the highs, drying volume, and an inside bar. Independent of the Stage 2 screen — scans the whole universe on its own. Writes `docs/episodic_pivots.html`. |
| `stage2_screener.py` | Live daily run. Screens the current universe, flags VCP-tight names, writes `docs/index.html`. |
| `backtest_stage2.py` | Walk-forward backtest over a date range you supply. `--vcp-only` requires VCP tightness on top of Stage 2 before entering. Writes `docs/backtest_report.html`. |
| `fetch_universe.py` | Optional — pulls the official NSE Midcap150/Smallcap250 constituent CSVs directly. Run locally if you want exact index membership instead of the Chartink market-cap-band approach. |
| `data/universe.csv` | Universe (symbol, name, category). **You update this manually** — see below. |

## Universe updates — manual via Chartink

You're scanning in Chartink's UI directly and exporting CSV, so there's no
scraper in this repo. Weekly (or whatever cadence you like):

1. Run the midcap scan and smallcap scan in Chartink (parameters below).
2. Export each result set, keep Symbol + Name columns.
3. Add a `category` column (`midcap` / `smallcap`), stack both exports
   under one header: `symbol,name,category`.
4. Paste over `data/universe.csv`, commit, push.

**Scan parameters** (AMFI cutoffs as of July 2026 — these shift every 6
months at the Jan/July reclassification, so re-check before your next refresh):

*Midcap:*
```
( {cash} ( latest close > 20 and latest volume > 100000 and latest market cap > 33500 and latest market cap <= 106300 ) )
```

*Smallcap:*
```
( {cash} ( latest close > 20 and latest volume > 100000 and latest market cap > 500 and latest market cap <= 33500 ) )
```

The volume condition here is just a coarse pre-filter — `stage2_screener.py`
re-checks 20-day average turnover (₹5 Cr threshold) properly downstream, so
it doesn't need to be exact.

## How the pieces fit together

```
Weekly (manual)   Chartink scan  ──►  data/universe.csv
Daily             universe.csv   ──►  stage2_screener.py  ──►  docs/index.html  (Stage 2 + VCP flag)
On demand         universe.csv   ──►  backtest_stage2.py  ──►  docs/backtest_report.html
```

## Setup

1. **Enable GitHub Pages**, serving from `docs/` on the default branch.
   Live dashboard: `https://<user>.github.io/<repo>/`
   Backtest report: `https://<user>.github.io/<repo>/backtest_report.html`

2. **Push to GitHub.** Two workflows included:
   - `screen.yml` — runs daily at 18:00 IST, screens the current universe.
   - `backtest.yml` — manual trigger only (Actions tab → "Run Backtest" → enter dates).

3. To run locally:
   ```bash
   pip install -r requirements.txt
   python stage2_screener.py
   python backtest_stage2.py --start 2022-01-01 --end 2024-12-31 --vcp-only
   ```

## The VCP overlay — what it actually checks

Runs only on names that already pass the Stage 2 Trend Template (VCP is an
entry-timing filter on top of trend confirmation, not a replacement for it).
A name is flagged `vcp_pass` when ALL of:

1. **Contracting swings** — a zigzag pivot detector finds the last 2–4
   pullback legs within a 90-day window, and each is shallower than the
   one before it (with 15% tolerance for noise).
2. **Volume dry-up** — 10-day average volume is below 70% of the 50-day
   average.
3. **Range tightness** — 10-day ATR is below 65% of the 50-day ATR.

**This is a heuristic proxy, not a reproduction of Minervini's actual
methodology** — real VCP identification involves visual judgment of chart
structure (does the base look orderly, are closes clustering near highs,
is the final contraction genuinely tight) that isn't fully reducible to a
formula. Treat `vcp_pass` as "worth pulling up the chart", not an automatic
signal. I tested the detector against engineered synthetic data before
shipping it (a series built with 3 deliberately shrinking pullbacks +
draining volume correctly flagged `vcp_pass=True`; a random walk with the
same volatility correctly flagged `False`), but I have not validated it
against real historical VCP setups you'd recognize by eye — worth
spot-checking the first few flagged names against actual charts.

Tunable at the top of `vcp_detector.py`: `ZIGZAG_MIN_PCT` (swing size to
count as a pivot), `LEG_TOLERANCE` (how strictly legs must shrink),
`VOL_DRYUP_RATIO`, `ATR_TIGHT_RATIO`.

## Episodic pivot scanner — what it actually checks

Independent of the Stage 2 screen (doesn't require the stock to be in a
Stage 2 uptrend first) — scans the whole universe for this exact sequence:

1. **Range expansion day** — single-day close-to-close gain > 6.5%,
   somewhere in the last 30 trading days.
2. **Volume confirmation** — that day's volume > 3x the 50-day EMA of
   volume at the time.
3. **Peak** — the high may print on the expansion day itself or anywhere
   in the following 4 trading days; peak = the max high across that
   whole window (not necessarily day 0).
4. **Held the move** — from the peak onward, the **closing** price has
   not fallen more than 20% below the peak high, at any point up to today.
5. **Volume dry-up** — the 50-day EMA of volume (the smoothed line, not
   raw daily volume) is in a steady decline from the peak to today.
6. **Inside bar** — at least one day between the peak and today has its
   entire high/low range contained inside the prior day's range. Any
   such day within the window counts, not just the latest bar.

All six conditions must hold for a hit. I validated this against
engineered synthetic data before shipping it: a series built with an
exact +8% expansion day at ~4x volume, a peak 3 days later, an inside
bar right after, and steadily declining volume through a 20-day
consolidation was caught with the exact right dates. A pure random walk
with no engineered pattern produced zero hits. That's a logic-correctness
check, not validation against real historical setups you'd recognize by
eye — worth spot-checking the first batch of real hits against actual charts.

Tunable at the top of `episodic_pivot_scanner.py`: `MIN_PCT_GAIN`,
`VOL_MULTIPLE`, `PEAK_WINDOW_DAYS`, `MAX_DRAWDOWN_PCT`, and the
`is_declining()` tolerance parameters if the volume dry-up check feels
too strict or too loose in practice.

## Backtest — what it does and does not model

**Mechanics:** weekly walk-forward, no lookahead. Screens using only bars
up to and including each rebalance date, takes top `--max-positions` by
RS Rating (optionally filtered to VCP-tight names only via `--vcp-only`),
holds equal-weight until the next rebalance, marks to market, re-screens.
A benchmark line (equal-weight of the entire fetched universe, unscreened)
runs alongside so you can see whether the screen is adding value at all.

**What it does NOT model:**
- **Survivorship bias** — today's `universe.csv` applied retroactively.
  Delisted/dropped names won't appear as candidates even though they were
  real options at the time. Tends to flatter results somewhat.
- **Transaction costs** — off by default, pass `--cost-bps 20` (or your
  real brokerage + STT + slippage estimate) to include them.
- **Intraday timing** — entries/exits assumed at the week's close.
- **Position sizing beyond equal-weight.**

Run both `--vcp-only` and without it over the same date range to see
whether the VCP filter actually improves the numbers or just cuts your
sample size with no edge — that comparison is the useful thing to look at
before trusting the overlay.

## Tuning

Trend/liquidity/RS thresholds: top of `screener_core.py`.
VCP thresholds: top of `vcp_detector.py`.

## Not yet built

- **Local CSV cache** — no fallback if yfinance rate-limits during a full
  universe run.
