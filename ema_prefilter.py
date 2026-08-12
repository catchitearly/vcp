"""
Stage 1: cheap EMA-50 pre-filter.

Fetches a short price history per symbol, computes the standard trailing
50-day EMA, and keeps only symbols where today's close is above it. This
runs BEFORE the expensive full-history curve fit so we only pay for the
24-month fetch + quadratic search on names that already clear the EMA bar.
"""

import time
import pandas as pd
import yfinance as yf

import config


def fetch_short_history(symbol: str):
    try:
        df = yf.download(
            symbol,
            period=f"{config.EMA_PREFETCH_MONTHS}mo",
            interval="1d",
            progress=False,
            auto_adjust=True,
        )
    except Exception as e:
        print(f"[warn] prefilter fetch failed for {symbol}: {e}")
        return None

    if df is None or df.empty or len(df) < config.EMA_PERIOD:
        return None
    return df


def passes_ema_filter(df: pd.DataFrame) -> bool:
    close = df["Close"]
    ema50 = close.ewm(span=config.EMA_PERIOD, adjust=False).mean()
    return float(close.iloc[-1]) > float(ema50.iloc[-1])


def passes_liquidity_filter(df: pd.DataFrame) -> bool:
    avg_vol = df["Volume"].tail(20).mean()
    return avg_vol >= config.MIN_AVG_VOLUME


def run_prefilter(symbols: list) -> list:
    survivors = []
    for i, symbol in enumerate(symbols, 1):
        df = fetch_short_history(symbol)
        if df is None:
            continue
        if not passes_liquidity_filter(df):
            continue
        if passes_ema_filter(df):
            survivors.append(symbol)
        if i % 25 == 0:
            print(f"[prefilter] {i}/{len(symbols)} scanned, {len(survivors)} survivors so far")
        time.sleep(0.15)  # be gentle with yfinance rate limits

    print(f"[prefilter] done: {len(survivors)}/{len(symbols)} passed EMA + liquidity filter")
    return survivors
