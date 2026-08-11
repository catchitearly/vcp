"""
screener_core.py

Shared logic for the Stage 2 Trend Template + liquidity screen.
Used by BOTH stage2_screener.py (live daily run) and backtest_stage2.py
(historical walk-forward simulation) so the two never drift apart.

Every evaluation function takes an `end_index` (index into the price lists)
so it can be evaluated "as of" any historical bar, not just the latest one.
This is what makes the backtest leak-free - on backtest date D, only bars
up to and including D are ever looked at.

Pure Python. yfinance used only as a download client; results are converted
to plain lists immediately.
"""

import csv
import datetime as dt

import yfinance as yf

# ---------------- CONFIG (shared defaults, overridable by callers) ----------------
LOOKBACK_DAYS = 420
SMA200_SLOPE_LOOKBACK = 20
RS_QUARTER_DAYS = 63

MIN_PRICE = 20
MIN_AVG_VOLUME_20D = 100_000
MIN_AVG_TURNOVER_20D = 50_000_000

PCT_ABOVE_52W_LOW = 0.30
PCT_WITHIN_52W_HIGH = 0.25
MIN_RS_RATING = 70


# ---------------- DATA LOADING ----------------

def load_universe(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def fetch_ohlcv(symbol_nse, start=None, end=None):
    """Fetch daily OHLCV via yfinance, convert immediately to plain lists.

    If start/end are omitted, pulls the last LOOKBACK_DAYS days (for live use).
    For backtesting, pass an explicit start well before the backtest window
    (need >=252 trading days of history before the first evaluation date).
    """
    ticker = f"{symbol_nse}.NS"
    if end is None:
        end = dt.date.today()
    if start is None:
        start = end - dt.timedelta(days=LOOKBACK_DAYS)

    try:
        df = yf.download(
            ticker, start=start.isoformat(), end=end.isoformat(),
            progress=False, auto_adjust=True, threads=False,
        )
    except Exception as exc:
        print(f"[WARN] fetch failed for {ticker}: {exc}")
        return None

    if df is None or df.empty or len(df) < 210:
        return None

    if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
        df.columns = [c[0] for c in df.columns]

    dates = [d.strftime("%Y-%m-%d") for d in df.index.to_pydatetime()]
    closes = [float(x) for x in df["Close"].tolist()]
    highs = [float(x) for x in df["High"].tolist()]
    lows = [float(x) for x in df["Low"].tolist()]
    volumes = [int(x) for x in df["Volume"].tolist()]

    del df

    return {"dates": dates, "close": closes, "high": highs, "low": lows, "volume": volumes}


# ---------------- INDICATORS (pure python) ----------------

def sma(values, period, end_index):
    """SMA of `values` ending at end_index (inclusive), lookback = period."""
    start = end_index - period + 1
    if start < 0:
        return None
    window = values[start:end_index + 1]
    return sum(window) / period


def sma_series(values, period, end_index, n_points):
    """Last n_points SMA values, each ending at successive indices up to end_index."""
    out = []
    for i in range(end_index - n_points + 1, end_index + 1):
        out.append(sma(values, period, end_index=i))
    return out


def pct_change(new, old):
    if old == 0:
        return 0.0
    return (new - old) / old


def date_index_on_or_before(dates, target_date):
    """Return the largest index i such that dates[i] <= target_date (ISO strings).
    Returns None if no such index exists (target_date before all data)."""
    idx = None
    for i, d in enumerate(dates):
        if d <= target_date:
            idx = i
        else:
            break
    return idx


# ---------------- TREND TEMPLATE ----------------

def evaluate_trend_template(data, end_index):
    closes = data["close"]
    if end_index < 251 or end_index >= len(closes):
        return False, {}

    price = closes[end_index]
    sma50 = sma(closes, 50, end_index)
    sma150 = sma(closes, 150, end_index)
    sma200 = sma(closes, 200, end_index)

    if None in (sma50, sma150, sma200):
        return False, {}

    sma200_series = sma_series(closes, 200, end_index, SMA200_SLOPE_LOOKBACK)
    sma200_rising = all(v is not None for v in sma200_series) and sma200_series[-1] > sma200_series[0]

    window_start = end_index - 251
    low_52w = min(data["low"][window_start:end_index + 1])
    high_52w = max(data["high"][window_start:end_index + 1])

    above_52w_low = price >= low_52w * (1 + PCT_ABOVE_52W_LOW)
    within_52w_high = price >= high_52w * (1 - PCT_WITHIN_52W_HIGH)

    checks = {
        "price_above_sma150": price > sma150,
        "price_above_sma200": price > sma200,
        "sma150_above_sma200": sma150 > sma200,
        "sma200_rising": sma200_rising,
        "sma50_above_sma150_sma200": sma50 > sma150 and sma50 > sma200,
        "price_above_sma50": price > sma50,
        "above_52w_low_by_30pct": above_52w_low,
        "within_25pct_of_52w_high": within_52w_high,
    }

    passes = all(checks.values())
    detail = {
        "price": round(price, 2),
        "sma50": round(sma50, 2),
        "sma150": round(sma150, 2),
        "sma200": round(sma200, 2),
        "low_52w": round(low_52w, 2),
        "high_52w": round(high_52w, 2),
        "pct_above_low": round(pct_change(price, low_52w) * 100, 1),
        "pct_below_high": round(pct_change(price, high_52w) * 100, 1),
        "checks": checks,
    }
    return passes, detail


# ---------------- LIQUIDITY ----------------

def evaluate_liquidity(data, end_index):
    if end_index < 19:
        return False, {}
    price = data["close"][end_index]
    vol_20 = data["volume"][end_index - 19:end_index + 1]
    close_20 = data["close"][end_index - 19:end_index + 1]
    avg_vol_20 = sum(vol_20) / len(vol_20)
    avg_turnover_20 = sum(c * v for c, v in zip(close_20, vol_20)) / len(vol_20)

    passes = (
        price >= MIN_PRICE
        and avg_vol_20 >= MIN_AVG_VOLUME_20D
        and avg_turnover_20 >= MIN_AVG_TURNOVER_20D
    )
    return passes, {
        "avg_vol_20d": int(avg_vol_20),
        "avg_turnover_20d_cr": round(avg_turnover_20 / 1e7, 2),
    }


# ---------------- RS RATING (IBD-style, ranked within universe) ----------------

def raw_rs_score(closes, end_index):
    if end_index < 251:
        return None
    price_now = closes[end_index]

    def ret(days_back):
        idx = end_index - days_back
        if idx < 0:
            return 0.0
        return pct_change(price_now, closes[idx])

    r1 = ret(RS_QUARTER_DAYS)
    r2 = ret(RS_QUARTER_DAYS * 2)
    r3 = ret(RS_QUARTER_DAYS * 3)
    r4 = ret(RS_QUARTER_DAYS * 4)

    return (2 * r1 + r2 + r3 + r4) / 5


def assign_rs_ratings(scored_symbols):
    """scored_symbols: list of (symbol, raw_score or None). Returns dict symbol -> percentile 1-99."""
    valid = [(s, sc) for s, sc in scored_symbols if sc is not None]
    valid.sort(key=lambda x: x[1])
    n = len(valid)
    ratings = {}
    for i, (sym, _) in enumerate(valid):
        pct_rank = (i + 1) / n * 99
        ratings[sym] = round(pct_rank)
    return ratings


# ---------------- COMBINED "SCREEN AS OF DATE" ----------------

def screen_universe_as_of(fetched, end_indices):
    """
    fetched: dict symbol -> data (from fetch_ohlcv)
    end_indices: dict symbol -> end_index to evaluate at (already resolved per-symbol
                 date alignment, since different stocks can have gaps on the same date)

    Returns list of result dicts, same shape as the live screener's output.
    """
    raw_scores = []
    for sym, data in fetched.items():
        ei = end_indices.get(sym)
        if ei is None:
            continue
        raw_scores.append((sym, raw_rs_score(data["close"], ei)))
    rs_ratings = assign_rs_ratings(raw_scores)

    results = []
    for sym, data in fetched.items():
        ei = end_indices.get(sym)
        if ei is None:
            continue
        trend_pass, trend_detail = evaluate_trend_template(data, ei)
        liq_pass, liq_detail = evaluate_liquidity(data, ei)
        rs_rating = rs_ratings.get(sym, 0)
        rs_pass = rs_rating >= MIN_RS_RATING
        overall_pass = trend_pass and liq_pass and rs_pass

        results.append({
            "symbol": sym,
            "stage2_pass": overall_pass,
            "trend_template_pass": trend_pass,
            "liquidity_pass": liq_pass,
            "rs_rating": rs_rating,
            "rs_pass": rs_pass,
            **trend_detail,
            **liq_detail,
        })
    return results
