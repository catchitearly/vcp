"""
backtest_stage2.py

Walk-forward backtest of the Stage 2 Trend Template + liquidity screen.

Mechanics (long-only, equal-weight, weekly rebalance):
  - At each rebalance date D, run the SAME screen_universe_as_of() logic
    used by the live screener, using ONLY price bars up to and including D
    (no lookahead - screener_core's end_index-based design guarantees this).
  - Take the top `--max-positions` passing names by RS Rating.
  - Hold them equal-weight until the next rebalance date, mark-to-market
    on that date's close, then re-screen and rebalance again.
  - A "benchmark" line is also computed: equal-weight buy of the ENTIRE
    fetched universe (no screening at all), rebalanced on the same weekly
    dates. This isolates whether the Stage 2 + liquidity screen is adding
    value over just holding the liquid mid/smallcap universe.

CAVEATS (read before trusting the numbers):
  - Survivorship bias: uses TODAY's universe.csv applied retroactively.
    Stocks that were delisted / dropped out of mid+smallcap over the
    backtest window won't appear as candidates even though they were
    real, investable options at the time.
  - No transaction costs, slippage, or STT/brokerage modeled by default
    (see --cost-bps to add a flat round-trip cost estimate).
  - Weekly granularity only - entries/exits assumed to happen exactly at
    that week's close, not intraday.

Usage:
    python backtest_stage2.py --start 2022-01-01 --end 2024-12-31
    python backtest_stage2.py --start 2023-06-01 --end 2024-06-01 --max-positions 8 --cost-bps 20
"""

import argparse
import datetime as dt
import json
import os

import screener_core as core
import vcp_detector as vcp

UNIVERSE_FILE = "data/universe.csv"
OUTPUT_DIR = "docs"
OUTPUT_HTML = os.path.join(OUTPUT_DIR, "backtest_report.html")
OUTPUT_JSON = os.path.join(OUTPUT_DIR, "backtest_results.json")

HISTORY_BUFFER_DAYS = 420  # enough for 252-day 52w window + 200 SMA + slope check, before the backtest start


def daterange_weekly(start, end):
    dates = []
    d = start
    while d <= end:
        dates.append(d)
        d += dt.timedelta(days=7)
    if dates[-1] != end:
        dates.append(end)
    return dates


def resolve_end_index(data, target_date_iso):
    return core.date_index_on_or_before(data["dates"], target_date_iso)


def period_return(fetched, symbols, idx0_map, idx1_map):
    """Equal-weight return of `symbols` from idx0 to idx1 (close-to-close)."""
    rets = []
    for sym in symbols:
        data = fetched.get(sym)
        if data is None:
            continue
        i0 = idx0_map.get(sym)
        i1 = idx1_map.get(sym)
        if i0 is None or i1 is None:
            continue
        p0 = data["close"][i0]
        p1 = data["close"][i1]
        if p0 <= 0:
            continue
        rets.append((p1 - p0) / p0)
    if not rets:
        return 0.0, 0
    return sum(rets) / len(rets), len(rets)


def max_drawdown(equity_curve):
    peak = equity_curve[0]
    mdd = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (v - peak) / peak
        if dd < mdd:
            mdd = dd
    return mdd


def run_backtest(start_date, end_date, max_positions, cost_bps, capital, vcp_only=False):
    universe = core.load_universe(UNIVERSE_FILE)
    print(f"Loaded {len(universe)} symbols from universe.")

    fetch_start = start_date - dt.timedelta(days=HISTORY_BUFFER_DAYS)

    fetched = {}
    meta = {}
    for row in universe:
        symbol = row["symbol"]
        data = core.fetch_ohlcv(symbol, start=fetch_start, end=end_date + dt.timedelta(days=1))
        if data is not None:
            fetched[symbol] = data
            meta[symbol] = row
        else:
            print(f"[SKIP] insufficient/no data: {symbol}")

    print(f"Fetched usable history for {len(fetched)}/{len(universe)} symbols.")

    rebalance_dates = daterange_weekly(start_date, end_date)
    rebalance_iso = [d.isoformat() for d in rebalance_dates]

    # Pre-resolve each symbol's bar index for every rebalance date once.
    idx_by_date = {}
    for diso in rebalance_iso:
        idx_by_date[diso] = {
            sym: resolve_end_index(data, diso) for sym, data in fetched.items()
        }

    strategy_equity = [capital]
    benchmark_equity = [capital]
    periods = []

    cost_frac = cost_bps / 10000.0

    for i in range(len(rebalance_iso) - 1):
        d0, d1 = rebalance_iso[i], rebalance_iso[i + 1]
        idx0_map, idx1_map = idx_by_date[d0], idx_by_date[d1]

        # Screen as of d0 using only data up to d0 (no lookahead).
        screenable = {sym: fetched[sym] for sym in fetched if idx0_map.get(sym) is not None}
        end_indices = {sym: idx0_map[sym] for sym in screenable}
        results = core.screen_universe_as_of(screenable, end_indices)
        passing = [r for r in results if r["stage2_pass"]]

        if vcp_only:
            vcp_filtered = []
            for r in passing:
                data = fetched[r["symbol"]]
                ei = idx0_map[r["symbol"]]
                vcp_pass, _ = vcp.evaluate_vcp(data, ei)
                if vcp_pass:
                    vcp_filtered.append(r)
            passing = vcp_filtered

        passing.sort(key=lambda r: r["rs_rating"], reverse=True)
        selection = [r["symbol"] for r in passing[:max_positions]]

        strat_ret, strat_n = period_return(fetched, selection, idx0_map, idx1_map)
        if selection:
            strat_ret -= cost_frac  # flat round-trip cost applied per rebalance when holding positions

        all_symbols = [sym for sym in fetched if idx0_map.get(sym) is not None]
        bench_ret, bench_n = period_return(fetched, all_symbols, idx0_map, idx1_map)

        strategy_equity.append(strategy_equity[-1] * (1 + strat_ret))
        benchmark_equity.append(benchmark_equity[-1] * (1 + bench_ret))

        periods.append({
            "date": d0,
            "next_date": d1,
            "selection": selection,
            "n_positions": strat_n,
            "period_return_pct": round(strat_ret * 100, 2),
            "benchmark_return_pct": round(bench_ret * 100, 2),
            "strategy_equity": round(strategy_equity[-1], 2),
            "benchmark_equity": round(benchmark_equity[-1], 2),
        })

    total_days = (end_date - start_date).days or 1
    strat_total_return = strategy_equity[-1] / capital - 1
    bench_total_return = benchmark_equity[-1] / capital - 1
    strat_cagr = (strategy_equity[-1] / capital) ** (365.0 / total_days) - 1
    bench_cagr = (benchmark_equity[-1] / capital) ** (365.0 / total_days) - 1
    strat_mdd = max_drawdown(strategy_equity)
    bench_mdd = max_drawdown(benchmark_equity)
    win_periods = [p for p in periods if p["period_return_pct"] > 0]
    win_rate = len(win_periods) / len(periods) if periods else 0.0
    avg_positions = sum(p["n_positions"] for p in periods) / len(periods) if periods else 0.0

    summary = {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "capital": capital,
        "max_positions": max_positions,
        "cost_bps_per_rebalance": cost_bps,
        "strategy_final_equity": round(strategy_equity[-1], 2),
        "benchmark_final_equity": round(benchmark_equity[-1], 2),
        "strategy_total_return_pct": round(strat_total_return * 100, 2),
        "benchmark_total_return_pct": round(bench_total_return * 100, 2),
        "strategy_cagr_pct": round(strat_cagr * 100, 2),
        "benchmark_cagr_pct": round(bench_cagr * 100, 2),
        "strategy_max_drawdown_pct": round(strat_mdd * 100, 2),
        "benchmark_max_drawdown_pct": round(bench_mdd * 100, 2),
        "win_rate_pct": round(win_rate * 100, 1),
        "avg_positions_held": round(avg_positions, 1),
        "n_rebalances": len(periods),
        "universe_size": len(universe),
        "symbols_with_data": len(fetched),
    }

    return summary, periods, rebalance_iso, strategy_equity, benchmark_equity


def write_html(summary, periods, rebalance_iso, strategy_equity, benchmark_equity):
    dates_json = json.dumps(rebalance_iso)
    strat_json = json.dumps([round(v, 2) for v in strategy_equity])
    bench_json = json.dumps([round(v, 2) for v in benchmark_equity])

    def period_row(p):
        sel = ", ".join(p["selection"][:8]) + (" ..." if len(p["selection"]) > 8 else "")
        return f"""
        <tr>
          <td>{p['date']}</td>
          <td>{p['n_positions']}</td>
          <td>{p['period_return_pct']}%</td>
          <td>{p['benchmark_return_pct']}%</td>
          <td>{p['strategy_equity']:,}</td>
          <td class="sel">{sel if sel.strip() else '&mdash;'}</td>
        </tr>"""

    period_rows = "\n".join(period_row(p) for p in periods)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Stage 2 Backtest Report</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/plotly.js/2.32.0/plotly.min.js"></script>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0f1117; color:#e6e6e6; margin:0; padding:24px; }}
  h1 {{ font-size:1.4rem; margin-bottom:4px; }}
  .meta {{ color:#9aa0a6; font-size:0.85rem; margin-bottom:20px; }}
  .stats {{ display:flex; flex-wrap:wrap; gap:12px; margin-bottom:24px; }}
  .stat-card {{ background:#1a1d24; border-radius:8px; padding:12px 16px; min-width:150px; }}
  .stat-card .label {{ color:#9aa0a6; font-size:0.75rem; text-transform:uppercase; }}
  .stat-card .value {{ font-size:1.3rem; font-weight:600; margin-top:4px; }}
  .value.pos {{ color:#4ade80; }}
  .value.neg {{ color:#f87171; }}
  #chart {{ margin-bottom:24px; }}
  table {{ border-collapse: collapse; width:100%; font-size:0.8rem; }}
  th, td {{ padding:6px 10px; text-align:left; border-bottom:1px solid #2a2d34; }}
  th {{ background:#1a1d24; position:sticky; top:0; }}
  .sel {{ color:#9aa0a6; }}
  .caveat {{ background:#241c1c; border-left:3px solid #f87171; padding:10px 14px; margin-bottom:20px; font-size:0.8rem; color:#d1a3a3; }}
  a {{ color:#7cb0ff; }}
</style>
</head>
<body>
  <h1>Stage 2 Strategy Backtest &mdash; NSE Mid/Small Cap</h1>
  <div class="meta">
    {summary['start']} to {summary['end']} &middot; {summary['n_rebalances']} weekly rebalances &middot;
    max {summary['max_positions']} positions &middot; {summary['cost_bps_per_rebalance']} bps cost/rebalance &middot;
    <a href="index.html">&larr; Live screener</a>
  </div>

  <div class="caveat">
    <strong>Read before trusting these numbers:</strong> this backtest uses today's universe.csv
    applied retroactively (survivorship bias &mdash; delisted/dropped stocks won't appear as
    candidates even though they were real options at the time), weekly granularity only,
    and {'no' if summary['cost_bps_per_rebalance'] == 0 else f"a flat {summary['cost_bps_per_rebalance']} bps"}
    transaction cost assumption.
  </div>

  <div class="stats">
    <div class="stat-card"><div class="label">Strategy Total Return</div>
      <div class="value {'pos' if summary['strategy_total_return_pct']>=0 else 'neg'}">{summary['strategy_total_return_pct']}%</div></div>
    <div class="stat-card"><div class="label">Benchmark Total Return</div>
      <div class="value {'pos' if summary['benchmark_total_return_pct']>=0 else 'neg'}">{summary['benchmark_total_return_pct']}%</div></div>
    <div class="stat-card"><div class="label">Strategy CAGR</div>
      <div class="value {'pos' if summary['strategy_cagr_pct']>=0 else 'neg'}">{summary['strategy_cagr_pct']}%</div></div>
    <div class="stat-card"><div class="label">Benchmark CAGR</div>
      <div class="value {'pos' if summary['benchmark_cagr_pct']>=0 else 'neg'}">{summary['benchmark_cagr_pct']}%</div></div>
    <div class="stat-card"><div class="label">Strategy Max Drawdown</div>
      <div class="value neg">{summary['strategy_max_drawdown_pct']}%</div></div>
    <div class="stat-card"><div class="label">Benchmark Max Drawdown</div>
      <div class="value neg">{summary['benchmark_max_drawdown_pct']}%</div></div>
    <div class="stat-card"><div class="label">Weekly Win Rate</div>
      <div class="value">{summary['win_rate_pct']}%</div></div>
    <div class="stat-card"><div class="label">Avg Positions Held</div>
      <div class="value">{summary['avg_positions_held']}</div></div>
  </div>

  <div id="chart" style="height:420px;"></div>

  <table>
    <thead>
      <tr><th>Date</th><th>Positions</th><th>Period Return</th><th>Benchmark Return</th><th>Equity</th><th>Holdings</th></tr>
    </thead>
    <tbody>
      {period_rows}
    </tbody>
  </table>

<script>
  const dates = {dates_json};
  const strat = {strat_json};
  const bench = {bench_json};

  Plotly.newPlot('chart', [
    {{ x: dates, y: strat, name: 'Stage 2 Strategy', type: 'scatter', mode: 'lines', line: {{color:'#4ade80', width:2}} }},
    {{ x: dates, y: bench, name: 'Benchmark (equal-weight universe)', type: 'scatter', mode: 'lines', line: {{color:'#7cb0ff', width:2, dash:'dot'}} }}
  ], {{
    paper_bgcolor: '#0f1117', plot_bgcolor: '#0f1117',
    font: {{ color: '#e6e6e6' }},
    margin: {{ t: 20, r: 20, l: 50, b: 40 }},
    xaxis: {{ gridcolor: '#2a2d34' }},
    yaxis: {{ gridcolor: '#2a2d34', title: 'Equity (Rs)' }},
    legend: {{ orientation: 'h', y: -0.2 }}
  }}, {{ responsive: true }});
</script>
</body>
</html>"""

    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    parser = argparse.ArgumentParser(description="Backtest the Stage 2 screener")
    parser.add_argument("--start", required=True, help="Backtest start date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Backtest end date, YYYY-MM-DD")
    parser.add_argument("--max-positions", type=int, default=10)
    parser.add_argument("--cost-bps", type=float, default=0.0,
                         help="Flat round-trip transaction cost in basis points, applied per rebalance when holding positions")
    parser.add_argument("--capital", type=float, default=1_000_000.0)
    parser.add_argument("--vcp-only", action="store_true",
                         help="Require VCP contraction+volume-dryup on top of Stage 2 trend template before entering a name")
    args = parser.parse_args()

    start_date = dt.date.fromisoformat(args.start)
    end_date = dt.date.fromisoformat(args.end)

    summary, periods, rebalance_iso, strategy_equity, benchmark_equity = run_backtest(
        start_date, end_date, args.max_positions, args.cost_bps, args.capital, vcp_only=args.vcp_only
    )
    summary["vcp_only"] = args.vcp_only

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump({"summary": summary, "periods": periods}, f, indent=2)

    write_html(summary, periods, rebalance_iso, strategy_equity, benchmark_equity)

    print(json.dumps(summary, indent=2))
    print(f"\nReport written to {OUTPUT_HTML}")


if __name__ == "__main__":
    main()
