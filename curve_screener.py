"""
Stage 2: rounding-bottom curve detection + OBV accumulation confirmation.
Runs only on symbols that survived the Stage 1 EMA-50 pre-filter.

Method: quadratic fit on log-close, searched across window lengths from
5 to 24 months (anchored at today), keeping whichever window scores best.
"""

import time
import numpy as np
import pandas as pd
import yfinance as yf

import config


def fetch_full_history(symbol: str):
    try:
        df = yf.Ticker(symbol).history(
            period=f"{config.FULL_FETCH_MONTHS}mo",
            interval="1d",
            auto_adjust=True,
        )
    except Exception as e:
        print(f"[warn] full fetch failed for {symbol}: {e}")
        return None

    min_days = config.MIN_WINDOW_MONTHS * config.TRADING_DAYS_PER_MONTH
    if df is None or df.empty or len(df) < min_days:
        return None

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    return df


def compute_obv(df: pd.DataFrame) -> pd.Series:
    close = df["Close"].values
    volume = df["Volume"].values
    obv = np.zeros(len(close))
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            obv[i] = obv[i - 1] + volume[i]
        elif close[i] < close[i - 1]:
            obv[i] = obv[i - 1] - volume[i]
        else:
            obv[i] = obv[i - 1]
    return pd.Series(obv, index=df.index)


def linreg_r2_slope(y: np.ndarray):
    x = np.arange(len(y))
    slope, intercept = np.polyfit(x, y, 1)
    fitted = slope * x + intercept
    ss_res = np.sum((y - fitted) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return slope, r2


def evaluate_window(log_close: np.ndarray):
    """Fit a quadratic to one candidate window of log-close prices."""
    w = len(log_close)
    x = np.arange(w)
    a, b, c = np.polyfit(x, log_close, 2)

    if a <= 0:
        return None  # not a U shape (declining-then-rising)

    fitted = a * x**2 + b * x + c
    resid = log_close - fitted
    ss_res = np.sum(resid**2)
    ss_tot = np.sum((log_close - np.mean(log_close)) ** 2)
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    if r2 < config.MIN_R2_PRICE:
        return None

    trough_x = -b / (2 * a)
    trough_frac = trough_x / w
    if not (config.TROUGH_FRAC_MIN <= trough_frac <= config.TROUGH_FRAC_MAX):
        return None

    residual_noise = float(np.std(resid))
    if residual_noise > config.MAX_RESIDUAL_NOISE:
        return None

    days_since_trough = w - trough_x
    if days_since_trough < config.MIN_FORMATION_DAYS:
        return None

    return {
        "window_len": w,
        "r2_price": r2,
        "trough_x": trough_x,
        "trough_frac": trough_frac,
        "residual_noise": residual_noise,
        "days_since_trough": days_since_trough,
        "coeffs": (a, b, c),
    }


def search_best_window(df: pd.DataFrame):
    log_close_full = np.log(df["Close"].values)
    n = len(log_close_full)

    min_w = config.MIN_WINDOW_MONTHS * config.TRADING_DAYS_PER_MONTH
    max_w = min(config.MAX_WINDOW_MONTHS * config.TRADING_DAYS_PER_MONTH, n)

    best = None
    for w in range(min_w, max_w + 1, config.WINDOW_STEP_TRADING_DAYS):
        segment = log_close_full[-w:]
        result = evaluate_window(segment)
        if result is None:
            continue
        if best is None or result["r2_price"] > best["r2_price"]:
            best = result

    return best


def confirm_obv(df: pd.DataFrame, window_len: int, trough_x: float):
    obv = compute_obv(df)
    obv_window = obv.values[-window_len:]

    slope, r2 = linreg_r2_slope(obv_window)
    if slope <= 0 or r2 < config.MIN_OBV_SLOPE_R2:
        return None

    # Bonus signal: OBV behaviour during the decline (left) segment only --
    # flat-to-rising OBV while price is still falling is the strongest
    # accumulation tell. Not a hard filter here, just reported for review.
    trough_idx = max(int(round(trough_x)), 2)
    decline_segment = obv_window[:trough_idx]
    decline_slope, decline_r2 = linreg_r2_slope(decline_segment)

    return {
        "obv_slope": slope,
        "obv_r2": r2,
        "obv_decline_slope": decline_slope,
        "obv_decline_r2": decline_r2,
    }


def screen_symbol(symbol: str):
    df = fetch_full_history(symbol)
    if df is None:
        return None

    best_window = search_best_window(df)
    if best_window is None:
        return None

    obv_result = confirm_obv(df, best_window["window_len"], best_window["trough_x"])
    if obv_result is None:
        return None

    trough_offset = best_window["window_len"] - int(round(best_window["trough_x"]))
    trough_date = df.index[-trough_offset]
    ema50 = df["Close"].ewm(span=config.EMA_PERIOD, adjust=False).mean().iloc[-1]

    score = best_window["r2_price"] * obv_result["obv_r2"]

    return {
        "symbol": symbol,
        "window_months": round(best_window["window_len"] / config.TRADING_DAYS_PER_MONTH, 1),
        "r2_price": round(best_window["r2_price"], 3),
        "trough_date": trough_date.strftime("%Y-%m-%d"),
        "days_since_trough": int(best_window["days_since_trough"]),
        "residual_noise": round(best_window["residual_noise"], 4),
        "obv_slope": round(obv_result["obv_slope"], 1),
        "obv_r2": round(obv_result["obv_r2"], 3),
        "obv_decline_slope": round(obv_result["obv_decline_slope"], 1),
        "last_close": round(float(df["Close"].iloc[-1]), 2),
        "ema50": round(float(ema50), 2),
        "score": round(score, 4),
    }


def run_screener(symbols: list) -> list:
    results = []
    for i, symbol in enumerate(symbols, 1):
        r = screen_symbol(symbol)
        if r is not None:
            results.append(r)
        if i % 10 == 0:
            print(f"[screener] {i}/{len(symbols)} evaluated, {len(results)} matches so far")
        time.sleep(0.15)

    results.sort(key=lambda r: r["score"], reverse=True)
    return results
