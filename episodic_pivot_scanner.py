"""
episodic_pivot_scanner.py

Scans for "episodic pivot" / power-play setups:

  1. Range expansion day - single-day close > +6.5% within the last
     `LOOKBACK_DAYS` trading days.
  2. Volume confirmation - that day's volume > `VOL_MULTIPLE`x the
     50-day EMA of volume (institutional-size participation).
  3. Peak identification - the high may print on the expansion day itself
     OR anywhere in the following `PEAK_WINDOW_DAYS` trading days; the
     peak is the max high across that whole window.
  4. Held the move - from the peak onward, the CLOSING price has not
     fallen more than `MAX_DRAWDOWN_PCT` below the peak high, at any
     point up to today.
  5. Volume dry-up - the 50-day EMA of volume itself is in a steady
     decline from the peak date to today (smoothed line, not raw daily
     volume, so single noisy days don't break the pattern).
  6. Inside bar - at least one day between the peak and today has its
     entire high-low range contained within the prior day's range
     (high[i] < high[i-1] and low[i] > low[i-1]). Any such day counts,
     not just the most recent bar.

Pure Python, no numpy/pandas. Reuses fetch_ohlcv / load_universe from
screener_core so it shares data-loading conventions with the rest of
the project.
"""

import csv
import datetime as dt
import json
import os

import screener_core as core

# ---------------- CONFIG ----------------
UNIVERSE_FILE = "data/universe.csv"
LOOKBACK_DAYS = 30          # trading days to search for an expansion day
MIN_PCT_GAIN = 6.5          # single-day close-to-close % gain threshold
VOL_EMA_PERIOD = 50
VOL_MULTIPLE = 3.0          # expansion-day volume must exceed this x EMA50(volume)
PEAK_WINDOW_DAYS = 4        # peak can form on day0 or within this many days after
MAX_DRAWDOWN_PCT = 20.0     # max allowed close-based drawdown from peak
DECLINE_TOLERANCE = 1.05    # allow ema50-vol to tick up to 5% day-over-day without breaking "declining"
MAX_VIOLATION_FRACTION = 0.25  # fraction of up-ticks tolerated within the "declining" window

OUTPUT_DIR = "docs"
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "episodic_pivots.json")
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "episodic_pivots.html")


# ---------------- EMA ----------------

def ema_series(values, period):
    """Standard EMA: seeded with SMA of the first `period` values, then
    recursive smoothing. Returns a list same length as `values`, with
    None for indices before the seed point."""
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
    `tolerance`x) are allowed - so it tolerates noise but rejects a
    genuine re-acceleration in volume."""
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

def find_episodic_pivots(data, symbol):
    dates, closes, highs, lows, volumes = (
        data["dates"], data["close"], data["high"], data["low"], data["volume"]
    )
    n = len(closes)
    if n < VOL_EMA_PERIOD + LOOKBACK_DAYS:
        return []

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

        # --- peak identification (day0 .. day0+PEAK_WINDOW_DAYS) ---
        window_end = min(day0 + PEAK_WINDOW_DAYS, n - 1)
        peak_idx = max(range(day0, window_end + 1), key=lambda i: highs[i])
        peak_high = highs[peak_idx]

        # --- held the move: closing price never >20% below peak, peak to today ---
        min_close_after_peak = min(closes[peak_idx:n])
        drawdown_pct = (peak_high - min_close_after_peak) / peak_high * 100
        if drawdown_pct > MAX_DRAWDOWN_PCT:
            continue

        # --- volume dry-up: EMA50(volume) declining from peak to today ---
        vol_ema_window = [v for v in vol_ema[peak_idx:n] if v is not None]
        if not is_declining(vol_ema_window):
            continue

        # --- inside bar: any day between peak+1 and today ---
        inside_bar_dates = []
        for i in range(peak_idx + 1, n):
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
            "peak_date": dates[peak_idx],
            "peak_high": round(peak_high, 2),
            "max_drawdown_from_peak_close_pct": round(drawdown_pct, 2),
            "current_close": round(closes[n - 1], 2),
            "inside_bar_dates": inside_bar_dates,
            "latest_inside_bar_date": inside_bar_dates[-1],
        })

    return results


# ---------------- MAIN ----------------

def main():
    universe = core.load_universe(UNIVERSE_FILE)
    print(f"Loaded {len(universe)} symbols from universe.")

    all_results = []
    for row in universe:
        symbol = row["symbol"]
        data = core.fetch_ohlcv(symbol)
        if data is None:
            print(f"[SKIP] insufficient/no data: {symbol}")
            continue
        hits = find_episodic_pivots(data, symbol)
        if hits:
            for h in hits:
                h["name"] = row.get("name", "")
                h["category"] = row.get("category", "")
            all_results.extend(hits)
            print(f"[HIT] {symbol}: {len(hits)} episodic pivot(s) found")

    all_results.sort(key=lambda r: r["expansion_date"], reverse=True)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": dt.datetime.now().isoformat(),
            "universe_size": len(universe),
            "hits_count": len(all_results),
            "results": all_results,
        }, f, indent=2)

    write_html(all_results)
    print(f"\nDone. {len(all_results)} episodic pivot setup(s) found across {len(universe)} symbols.")


def write_html(results):
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
        </tr>"""

    rows = "\n".join(row_html(r) for r in results)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Episodic Pivot Scanner</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:24px; }}
  h1 {{ font-size:1.4rem; margin-bottom:4px; }}
  .meta {{ color:#9aa0a6; font-size:0.85rem; margin-bottom:20px; }}
  table {{ border-collapse: collapse; width:100%; font-size:0.85rem; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid #2a2d34; }}
  th {{ background:#1a1d24; position:sticky; top:0; }}
  tr:hover {{ background:#1a1d24; }}
  .ib {{ color:#facc15; font-size:0.78rem; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background:#1f3a2e; color:#4ade80; font-size:0.8rem; }}
  a {{ color:#7cb0ff; }}
</style>
</head>
<body>
  <h1>Episodic Pivot Scanner</h1>
  <div class="meta">
    Generated {generated} &middot;
    <span class="badge">{len(results)} setups found</span>
    &middot; Expansion &gt;{MIN_PCT_GAIN}% on &gt;{VOL_MULTIPLE}x EMA50 volume, within last {LOOKBACK_DAYS} trading days
    &middot; Max {MAX_DRAWDOWN_PCT}% close-based drawdown from peak &middot; Requires volume dry-up + inside bar
    &middot; <a href="index.html">&larr; Stage 2 screener</a>
  </div>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Name</th><th>Expansion Date</th><th>Gain</th><th>Vol Multiple</th>
        <th>Peak Date</th><th>Peak High</th><th>Drawdown from Peak</th><th>Current Close</th><th>Inside Bar Date(s)</th>
      </tr>
    </thead>
    <tbody>
      {rows if rows else '<tr><td colspan="10">No setups found.</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
