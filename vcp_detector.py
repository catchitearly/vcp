"""
vcp_detector.py

Detects Volatility Contraction Pattern characteristics within names that
already pass the Stage 2 Trend Template screen:

  1. Sequential contracting swings - each pullback within the lookback
     window shallower than the one before it (measured via a zigzag
     pivot detector).
  2. Volume dry-up - recent average volume well below the base average,
     i.e. supply drying up as the stock tightens.
  3. Range tightness - recent (short-period) ATR meaningfully smaller
     than the base (longer-period) ATR.

This is a HEURISTIC proxy for VCP, not a reproduction of Minervini's
exact methodology (which involves visual judgment of chart structure
that isn't fully reducible to a formula). Treat a "vcp_pass" flag as
"worth pulling up the chart", not as an automatic buy signal.

Pure Python, no numpy/pandas. Takes the same {dates, close, high, low,
volume} dict + end_index convention as screener_core, so it composes
cleanly with both the live screener and the backtester.
"""

# ---------------- CONFIG ----------------
PIVOT_LOOKBACK = 90          # trading days to search for contraction legs
ZIGZAG_MIN_PCT = 0.03        # minimum swing size to register as a pivot (3%)
MAX_LEGS_CONSIDERED = 4      # only look at the most recent N down-legs
MIN_LEGS_REQUIRED = 2        # need at least this many legs to call it "contracting"
LEG_TOLERANCE = 1.15         # allow 15% slack when checking each leg <= previous leg

VOL_RECENT_DAYS = 10
VOL_BASE_DAYS = 50
VOL_DRYUP_RATIO = 0.70       # recent avg volume must be below 70% of base avg

ATR_RECENT_DAYS = 10
ATR_BASE_DAYS = 50
ATR_TIGHT_RATIO = 0.65       # recent ATR must be below 65% of base ATR


# ---------------- ZIGZAG PIVOT DETECTION ----------------

def find_pivots(data, end_index, lookback=PIVOT_LOOKBACK, min_pct_move=ZIGZAG_MIN_PCT):
    """Simple zigzag: returns a chronological list of {idx, price, type}
    pivots ('high' or 'low') within the lookback window ending at end_index."""
    start = max(0, end_index - lookback + 1)
    if end_index - start < 10:
        return []

    highs, lows = data["high"], data["low"]
    trend = "up"
    extreme_idx = start
    extreme_price = highs[start]
    pivots = []

    for i in range(start + 1, end_index + 1):
        h, l = highs[i], lows[i]
        if trend == "up":
            if h > extreme_price:
                extreme_price, extreme_idx = h, i
            elif l <= extreme_price * (1 - min_pct_move):
                pivots.append({"idx": extreme_idx, "price": extreme_price, "type": "high"})
                trend = "down"
                extreme_price, extreme_idx = l, i
        else:
            if l < extreme_price:
                extreme_price, extreme_idx = l, i
            elif h >= extreme_price * (1 + min_pct_move):
                pivots.append({"idx": extreme_idx, "price": extreme_price, "type": "low"})
                trend = "up"
                extreme_price, extreme_idx = h, i

    pivots.append({"idx": extreme_idx, "price": extreme_price, "type": "high" if trend == "up" else "low"})
    return pivots


def extract_down_legs(pivots):
    """From chronological pivots, extract high->low legs with % depth."""
    legs = []
    for a, b in zip(pivots, pivots[1:]):
        if a["type"] == "high" and b["type"] == "low" and a["price"] > 0:
            depth_pct = (a["price"] - b["price"]) / a["price"] * 100
            legs.append({
                "from_idx": a["idx"], "to_idx": b["idx"],
                "from_price": round(a["price"], 2), "to_price": round(b["price"], 2),
                "depth_pct": round(depth_pct, 2),
            })
    return legs


def is_contracting(legs, min_legs=MIN_LEGS_REQUIRED, max_legs=MAX_LEGS_CONSIDERED, tolerance=LEG_TOLERANCE):
    """Checks the most recent `max_legs` down-legs are non-increasing in
    depth (each leg no more than `tolerance`x the previous one), i.e. the
    stock is contracting into a tighter and tighter range over time."""
    recent = legs[-max_legs:] if len(legs) >= max_legs else legs
    if len(recent) < min_legs:
        return False
    for prev, curr in zip(recent, recent[1:]):
        if curr["depth_pct"] > prev["depth_pct"] * tolerance:
            return False
    return True


# ---------------- VOLUME DRY-UP ----------------

def volume_dryup_ratio(data, end_index, recent_n=VOL_RECENT_DAYS, base_n=VOL_BASE_DAYS):
    if end_index - base_n < 0:
        return None
    volumes = data["volume"]
    recent_avg = sum(volumes[end_index - recent_n + 1:end_index + 1]) / recent_n
    base_avg = sum(volumes[end_index - base_n + 1:end_index + 1]) / base_n
    if base_avg <= 0:
        return None
    return recent_avg / base_avg


# ---------------- RANGE TIGHTNESS (ATR) ----------------

def atr(data, end_index, period):
    start = end_index - period + 1
    if start - 1 < 0:
        return None
    highs, lows, closes = data["high"], data["low"], data["close"]
    trs = []
    for i in range(start, end_index + 1):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    return sum(trs) / period


def atr_tightness_ratio(data, end_index, recent_period=ATR_RECENT_DAYS, base_period=ATR_BASE_DAYS):
    atr_recent = atr(data, end_index, recent_period)
    atr_base = atr(data, end_index, base_period)
    if atr_recent is None or atr_base is None or atr_base <= 0:
        return None
    return atr_recent / atr_base


# ---------------- COMBINED EVALUATION ----------------

def evaluate_vcp(data, end_index):
    """Returns (passes: bool, detail: dict). `passes` requires all three:
    contracting swings, volume dry-up, and range tightness."""
    pivots = find_pivots(data, end_index)
    legs = extract_down_legs(pivots)
    contracting = is_contracting(legs)

    vol_ratio = volume_dryup_ratio(data, end_index)
    vol_dry = vol_ratio is not None and vol_ratio < VOL_DRYUP_RATIO

    atr_ratio = atr_tightness_ratio(data, end_index)
    tight = atr_ratio is not None and atr_ratio < ATR_TIGHT_RATIO

    passes = contracting and vol_dry and tight

    detail = {
        "contracting_legs": contracting,
        "n_legs_found": len(legs),
        "recent_legs_depth_pct": [l["depth_pct"] for l in legs[-MAX_LEGS_CONSIDERED:]],
        "volume_dryup_ratio": round(vol_ratio, 2) if vol_ratio is not None else None,
        "volume_dry": vol_dry,
        "atr_tightness_ratio": round(atr_ratio, 2) if atr_ratio is not None else None,
        "range_tight": tight,
        "vcp_pass": passes,
    }
    return passes, detail
