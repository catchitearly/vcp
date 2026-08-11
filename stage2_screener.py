"""
Stage 2 Trend Template Screener - live daily run.

Long-only. Uses screener_core for all indicator/rule logic (shared with
backtest_stage2.py so live and backtested results use identical rules).

Output:
- docs/index.html
- docs/screen_results.json
"""

import datetime as dt
import json
import os

import screener_core as core
import vcp_detector as vcp

UNIVERSE_FILE = "data/universe.csv"
OUTPUT_DIR = "docs"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "index.html")
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "screen_results.json")


def main():
    universe = core.load_universe(UNIVERSE_FILE)
    print(f"Loaded {len(universe)} symbols from universe.")

    fetched = {}
    meta = {}
    for row in universe:
        symbol = row["symbol"]
        data = core.fetch_ohlcv(symbol)
        if data is not None:
            fetched[symbol] = data
            meta[symbol] = row
        else:
            print(f"[SKIP] insufficient/no data: {symbol}")

    print(f"Fetched usable data for {len(fetched)} symbols.")

    end_indices = {sym: len(d["close"]) - 1 for sym, d in fetched.items()}
    results = core.screen_universe_as_of(fetched, end_indices)

    for r in results:
        row = meta.get(r["symbol"], {})
        r["name"] = row.get("name", "")
        r["category"] = row.get("category", "")

    # Run VCP contraction/volume-dryup detection only on names that already
    # pass Stage 2 - VCP is an entry-timing overlay on top of trend confirmation,
    # not a substitute for it.
    for r in results:
        if r["stage2_pass"]:
            data = fetched[r["symbol"]]
            end_index = end_indices[r["symbol"]]
            vcp_pass, vcp_detail = vcp.evaluate_vcp(data, end_index)
            r["vcp_pass"] = vcp_pass
            r["vcp_detail"] = vcp_detail
        else:
            r["vcp_pass"] = False
            r["vcp_detail"] = None

    passed = [r for r in results if r["stage2_pass"]]
    passed.sort(key=lambda r: (not r["vcp_pass"], -r["rs_rating"]))

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": dt.datetime.now().isoformat(),
            "universe_size": len(universe),
            "fetched": len(fetched),
            "passed_count": len(passed),
            "results": results,
        }, f, indent=2)

    write_html(results, passed)
    print(f"Done. {len(passed)}/{len(universe)} symbols passed Stage 2 + liquidity screen.")


def write_html(all_results, passed):
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M IST")

    def row_html(r):
        checks = r.get("checks", {})
        checks_str = ", ".join(k for k, v in checks.items() if not v) or "all pass"
        vd = r.get("vcp_detail")
        if r.get("vcp_pass"):
            vcp_str = '<span class="vcp-badge">VCP tight</span>'
        elif vd is not None:
            legs = vd.get("recent_legs_depth_pct", [])
            legs_str = "/".join(f"{x}%" for x in legs) if legs else "-"
            vcp_str = f'<span class="vcp-off">legs {legs_str}, vol {vd.get("volume_dryup_ratio")}, atr {vd.get("atr_tightness_ratio")}</span>'
        else:
            vcp_str = "-"
        return f"""
        <tr>
          <td><strong>{r['symbol']}</strong></td>
          <td>{r['name']}</td>
          <td>{r['category']}</td>
          <td>{r.get('price', '-')}</td>
          <td>{r.get('pct_above_low', '-')}%</td>
          <td>{r.get('pct_below_high', '-')}%</td>
          <td>{r['rs_rating']}</td>
          <td>{r.get('avg_turnover_20d_cr', '-')} Cr</td>
          <td>{vcp_str}</td>
          <td class="fail-detail">{checks_str}</td>
        </tr>"""

    passed_rows = "\n".join(row_html(r) for r in passed)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Stage 2 Screener - NSE Mid/Small Cap</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:24px; }}
  h1 {{ font-size:1.4rem; margin-bottom:4px; }}
  .meta {{ color:#9aa0a6; font-size:0.85rem; margin-bottom:20px; }}
  table {{ border-collapse: collapse; width:100%; font-size:0.85rem; }}
  th, td {{ padding:8px 10px; text-align:left; border-bottom:1px solid #2a2d34; }}
  th {{ background:#1a1d24; position:sticky; top:0; }}
  tr:hover {{ background:#1a1d24; }}
  .fail-detail {{ color:#6b7280; font-size:0.75rem; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background:#1f3a2e; color:#4ade80; font-size:0.8rem; }}
  .vcp-badge {{ display:inline-block; padding:2px 8px; border-radius:10px; background:#3a2e1f; color:#facc15; font-size:0.75rem; font-weight:600; }}
  .vcp-off {{ color:#6b7280; font-size:0.72rem; }}
  a {{ color:#7cb0ff; }}
</style>
</head>
<body>
  <h1>Stage 2 Trend Template Screener &mdash; NSE Mid/Small Cap</h1>
  <div class="meta">
    Generated {generated} &middot; Long-only &middot;
    <span class="badge">{len(passed)} passed</span> of {len(all_results)} screened
    &middot; Min RS Rating {core.MIN_RS_RATING} &middot; Min 20D turnover ₹{core.MIN_AVG_TURNOVER_20D/1e7:.0f} Cr
    &middot; <span class="vcp-badge">VCP tight</span> = contracting swings + volume dry-up + range tightness (heuristic, verify on chart)
    &middot; <a href="backtest_report.html">Backtest report</a> &middot; <a href="episodic_pivots.html">Episodic pivots &rarr;</a>
  </div>
  <table>
    <thead>
      <tr>
        <th>Symbol</th><th>Name</th><th>Cap</th><th>Price</th>
        <th>Above 52W Low</th><th>Below 52W High</th><th>RS Rating</th>
        <th>Avg Turnover (20D)</th><th>VCP</th><th>Notes</th>
      </tr>
    </thead>
    <tbody>
      {passed_rows if passed_rows else '<tr><td colspan="10">No symbols passed today.</td></tr>'}
    </tbody>
  </table>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


if __name__ == "__main__":
    main()
