"""
episodic_pivot_scanner.py

Scans for "episodic pivot" / power-play setups:

  1. Range expansion day - single-day close > +6.5% within the last
     `LOOKBACK_DAYS` trading days.
  2. Volume confirmation - that day's volume > `VOL_MULTIPLE`x the
     50-day EMA of volume (institutional-size participation).
  3. (Optional, toggle) Expansion-day volume must be the HIGHEST single-day
     volume in the trailing ~1 year (252 trading days) - off by default,
     enable with --require-yearly-high-volume.
  4. Peak identification - the high may print on the expansion day itself
     OR anywhere in the following `PEAK_WINDOW_DAYS` trading days; the
     peak is the max high across that whole window.
  5. Held the move - from the peak onward, the CLOSING price has not
     fallen more than `MAX_DRAWDOWN_PCT` below the peak high, at any
     point up to the as-of date.
  6. Volume dry-up - the 50-day EMA of volume itself is in a steady
     decline from the peak date to the as-of date.
  7. Inside bar - at least `MIN_GAP_DAYS` trading days after the expansion
     day, some day has its entire high-low range contained within the
     prior day's range (high[i] < high[i-1] and low[i] > low[i-1]).
     Any such day (subject to the gap) counts, not just the most recent bar.

DATE-RANGE MODE: by default this replays the scan across the trailing
`--days` (default 7) calendar days rather than just "today" - so a signal
isn't missed just because the scanner wasn't run on the exact day it first
qualified. Hits are deduped by (symbol, expansion_date), keeping the
freshest snapshot plus first/last seen as-of dates. Use --start/--end for
a custom historical range instead.

Pure Python, no numpy/pandas. Reuses fetch_ohlcv / load_universe /
date_index_on_or_before from screener_core.
"""

import argparse
import datetime as dt
import json
import os

import screener_core as core

# ---------------- CONFIG ----------------
UNIVERSE_FILE = "data/universe.csv"
LOOKBACK_DAYS = 30              # trading days to search for an expansion day, relative to as-of date
MIN_PCT_GAIN = 6.5              # single-day close-to-close % gain threshold
VOL_EMA_PERIOD = 50
VOL_MULTIPLE = 3.0              # expansion-day volume must exceed this x EMA50(volume)
YEARLY_HIGH_VOLUME_WINDOW = 252 # trading days treated as "1 year" for the optional yearly-high check
PEAK_WINDOW_DAYS = 4            # peak can form on day0 or within this many days after
MAX_DRAWDOWN_PCT = 20.0         # max allowed close-based drawdown from peak
MIN_GAP_DAYS = 5                # minimum trading-day gap between expansion day and a qualifying inside bar
DECLINE_TOLERANCE = 1.05        # allow ema50-vol to tick up to 5% day-over-day without breaking "declining"
MAX_VIOLATION_FRACTION = 0.25   # fraction of up-ticks tolerated within the "declining" window

OUTPUT_DIR = "docs"
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "episodic_pivots.json")
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "episodic_pivots.html")


# ---------------- EMA ----------------

def ema_series(values, period):
    """Standard EMA: seeded with SMA of the first `period` values, then
    recursive smoothing. Returns a list same length as `values`, with
    None for indices before the seed point. Causal (each point depends
    only on prior values), so it's safe to compute once per symbol over
    the full series and reuse across multiple as-of dates."""
    n = len(values)
    out = [None] * n
    if n < period:
        return out
    multiplier = 2.0 / (period + 1)
    seed = sum(values[0:period]) / period
    out[period - 1] = seed
    for i in range(period, n):
        out[i] = (values[i] - out[i - 1]) * multiplier + out[i - 1]
    return out


def is_declining(series, tolerance=DECLINE_TOLERANCE, max_violation_fraction=MAX_VIOLATION_FRACTION):
    """True if `series` is in a steady downtrend: endpoint clearly below
    start, and only a minority of day-over-day upticks (each capped at
    `tolerance`x) are allowed."""
    if len(series) < 3:
        return False
    if series[-1] >= series[0]:
        return False
    violations = 0
    for a, b in zip(series, series[1:]):
        if b > a * tolerance:
            violations += 1
    return violations <= max(1, int(len(series) * max_violation_fraction))


# ---------------- CORE SCAN ----------------

def find_episodic_pivots(data, symbol, as_of_index=None, require_yearly_vol_high=False, vol_ema=None):
    """Scan for episodic pivot setups using only bars up to and including
    `as_of_index` (defaults to the last available bar - i.e. "today").
    `vol_ema` can be precomputed once per symbol (full series) and passed
    in to avoid recomputing it for every as-of date in a replay."""
    dates, closes, highs, lows, volumes = (
        data["dates"], data["close"], data["high"], data["low"], data["volume"]
    )
    full_n = len(closes)
    if as_of_index is None:
        as_of_index = full_n - 1
    n = as_of_index + 1  # only bars [0, n) are visible as of this date

    if n < VOL_EMA_PERIOD + LOOKBACK_DAYS:
        return []

    if vol_ema is None:
        vol_ema = ema_series(volumes, VOL_EMA_PERIOD)

    results = []
    scan_start = max(VOL_EMA_PERIOD, n - LOOKBACK_DAYS)

    for day0 in range(scan_start, n):
        if day0 == 0 or vol_ema[day0] is None:
            continue

        pct_gain = (closes[day0] - closes[day0 - 1]) / closes[day0 - 1] * 100
        if pct_gain <= MIN_PCT_GAIN:
            continue
        if volumes[day0] <= VOL_MULTIPLE * vol_ema[day0]:
            continue

        if require_yearly_vol_high:
            yr_start = max(0, day0 - YEARLY_HIGH_VOLUME_WINDOW + 1)
            if volumes[day0] < max(volumes[yr_start:day0 + 1]):
                continue

        # --- peak identification (day0 .. day0+PEAK_WINDOW_DAYS, capped at as-of date) ---
        window_end = min(day0 + PEAK_WINDOW_DAYS, n - 1)
        peak_idx = max(range(day0, window_end + 1), key=lambda i: highs[i])
        peak_high = highs[peak_idx]

        # --- held the move: closing price never >20% below peak, peak to as-of date ---
        min_close_after_peak = min(closes[peak_idx:n])
        drawdown_pct = (peak_high - min_close_after_peak) / peak_high * 100
        if drawdown_pct > MAX_DRAWDOWN_PCT:
            continue

        # --- volume dry-up: EMA50(volume) declining from peak to as-of date ---
        vol_ema_window = [v for v in vol_ema[peak_idx:n] if v is not None]
        if not is_declining(vol_ema_window):
            continue

        # --- inside bar: any day at least MIN_GAP_DAYS trading days after day0 ---
        inside_bar_dates = []
        for i in range(peak_idx + 1, n):
            if (i - day0) < MIN_GAP_DAYS:
                continue
            if highs[i] < highs[i - 1] and lows[i] > lows[i - 1]:
                inside_bar_dates.append(dates[i])
        if not inside_bar_dates:
            continue

        results.append({
            "symbol": symbol,
            "expansion_date": dates[day0],
            "expansion_pct_gain": round(pct_gain, 2),
            "expansion_close": round(closes[day0], 2),
            "expansion_volume": volumes[day0],
            "volume_ema50_at_expansion": round(vol_ema[day0]),
            "volume_multiple": round(volumes[day0] / vol_ema[day0], 2),
            "yearly_high_volume_checked": require_yearly_vol_high,
            "peak_date": dates[peak_idx],
            "peak_high": round(peak_high, 2),
            "max_drawdown_from_peak_close_pct": round(drawdown_pct, 2),
            "current_close": round(closes[n - 1], 2),
            "as_of_date": dates[n - 1],
            "inside_bar_dates": inside_bar_dates,
            "latest_inside_bar_date": inside_bar_dates[-1],
        })

    return results


# ---------------- MAIN (date-range replay) ----------------

def daterange_calendar(start_date, end_date):
    dates = []
    d = start_date
    while d <= end_date:
        dates.append(d.isoformat())
        d += dt.timedelta(days=1)
    return dates


def main():
    parser = argparse.ArgumentParser(description="Episodic pivot scanner (date-range replay)")
    parser.add_argument("--start", help="Replay start date YYYY-MM-DD")
    parser.add_argument("--end", help="Replay end date YYYY-MM-DD (default: today)")
    parser.add_argument("--days", type=int, default=7,
                         help="If --start not given, replay the trailing N calendar days (default 7)")
    parser.add_argument("--require-yearly-high-volume", action="store_true",
                         help="Require expansion-day volume to be the highest single-day volume in the trailing ~1 year")
    args = parser.parse_args()

    end_date = dt.date.fromisoformat(args.end) if args.end else dt.date.today()
    start_date = dt.date.fromisoformat(args.start) if args.start else end_date - dt.timedelta(days=args.days)
    scan_dates = daterange_calendar(start_date, end_date)

    universe = core.load_universe(UNIVERSE_FILE)
    print(f"Loaded {len(universe)} symbols. Replaying {len(scan_dates)} calendar days: {start_date} to {end_date}")

    hits_by_key = {}  # (symbol, expansion_date) -> hit dict, freshest snapshot kept

    for row in universe:
        symbol = row["symbol"]
        data = core.fetch_ohlcv(symbol)
        if data is None:
            print(f"[SKIP] insufficient/no data: {symbol}")
            continue

        vol_ema = ema_series(data["volume"], VOL_EMA_PERIOD)  # compute once per symbol, reused across as-of dates

        for scan_date in scan_dates:
            idx = core.date_index_on_or_before(data["dates"], scan_date)
            if idx is None:
                continue
            found = find_episodic_pivots(
                data, symbol, as_of_index=idx,
                require_yearly_vol_high=args.require_yearly_high_volume,
                vol_ema=vol_ema,
            )
            for h in found:
                key = (h["symbol"], h["expansion_date"])
                h["name"] = row.get("name", "")
                h["category"] = row.get("category", "")
                if key not in hits_by_key:
                    h["first_seen_asof"] = scan_date
                    h["last_seen_asof"] = scan_date
                    hits_by_key[key] = h
                else:
                    first_seen = hits_by_key[key]["first_seen_asof"]
                    h["first_seen_asof"] = first_seen
                    h["last_seen_asof"] = scan_date
                    hits_by_key[key] = h  # keep freshest snapshot's details

        if any(k[0] == symbol for k in hits_by_key):
            print(f"[HIT] {symbol}")

    results = list(hits_by_key.values())
    results.sort(key=lambda r: r["volume_multiple"], reverse=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": dt.datetime.now().isoformat(),
            "replay_start": start_date.isoformat(),
            "replay_end": end_date.isoformat(),
            "require_yearly_high_volume": args.require_yearly_high_volume,
            "universe_size": len(universe),
            "hits_count": len(results),
            "results": results,
        }, f, indent=2)

    write_html(results, start_date, end_date, args.require_yearly_high_volume)
    print(f"\nDone. {len(results)} unique episodic pivot setup(s) found across {len(universe)} symbols.")


def write_html(results, start_date, end_date, require_yearly_high_volume):
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M IST")

    def row_html(r):
        ib_dates = ", ".join(r["inside_bar_dates"])
        return f"""
        <tr>
          <td><strong>{r['symbol']}</strong></td>
          <td>{r['name']}</td>
          <td>{r['expansion_date']}</td>
          <td>+{r['expansion_pct_gain']}%</td>
          <td>{r['volume_multiple']}x</td>
          <td>{r['peak_date']}</td>
          <td>{r['peak_high']}</td>
          <td>{r['max_drawdown_from_peak_close_pct']}%</td>
          <td>{r['current_close']}</td>
          <td class="ib">{ib_dates}</td>
          <td>{r['first_seen_asof']}</td>
          <td>{r['last_seen_asof']}</td>
        </tr>"""

    rows = "\n".join(row_html(r) for r in results)
    yv_note = "ON - expansion-day volume required to be the highest in the trailing ~1 year" if require_yearly_high_volume else "OFF"

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Episodic Pivot Scanner</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:24px; }}
  h1 {{ font-size:1.4rem; margin-bottom:4px; }}
  .meta {{ color:#9aa0a6; font-size:0.85rem; margin-bottom:20px; line-height:1.6; }}
  table {{ border-collapse: collapse; width:100%; font-size:0.82rem; }}
  th, td {{ padding:7px 9px; text-align:left; border-bottom:1px solid #2a2d34; }}
  th {{ background:#1a1d24; position:sticky; top:0; }}
  tr:hover {{ background:#1a1d24; }}
  .ib {{ color:#facc15; font-size:0.76rem; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background:#1f3a2e; color:#4ade80; font-size:0.8rem; }}
  a {{ color:#7cb0ff; }}
</style>
</head>
<body>
  <h1>Episodic Pivot Scanner</h1>
  <div class="meta">
    Generated {generated} &middot; Replay window: {start_date} to {end_date} &middot;
    <span class="badge">{len(results)} unique setups</span><br>
    Expansion &gt;{MIN_PCT_GAIN}% on &gt;{VOL_MULTIPLE}x EMA50 volume, within last {LOOKBACK_DAYS} trading days of each as-of date &middot;
    Max {MAX_DRAWDOWN_PCT}% close-based drawdown from peak &middot; Min {MIN_GAP_DAYS}-trading-day gap before a qualifying inside bar &middot;
    Yearly-high-volume filter: {yv_note} &middot; Sorted by volume multiple, descending
    &middot; <a href="index.html">&larr; Stage 2 screener</a>
  </div>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Name</th><th>Expansion Date</th><th>Gain</th><th>Vol Multiple</th>
        <th>Peak Date</th><th>Peak High</th><th>Drawdown from Peak</th><th>Current Close</th>
        <th>Inside Bar Date(s)</th><th>First Seen</th><th>Last Seen</th>
      </tr>
    </thead>
    <tbody>
      {rows if rows else '<tr><td colspan="12">No setups found in this window.</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
