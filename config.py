"""
Central config for the rounding-bottom + OBV accumulation screener.
Tune thresholds here rather than inside the logic files.
"""

# --- Universe ---
UNIVERSE_CSV = "data/universe.csv"  # column: symbol (NSE tickers, e.g. RELIANCE.NS)

# --- Stage 1: cheap EMA-50 pre-filter ---
EMA_PERIOD = 50
EMA_PREFETCH_MONTHS = 5      # short window just to compute a stable 50 EMA
MIN_AVG_VOLUME = 100_000     # liquidity filter (20-day avg volume), tune per universe

# --- Stage 2: curve + OBV screener (only runs on Stage-1 survivors) ---
TRADING_DAYS_PER_MONTH = 21

MIN_WINDOW_MONTHS = 5
MAX_WINDOW_MONTHS = 24
WINDOW_STEP_TRADING_DAYS = 10     # ~2 weeks between candidate window lengths
FULL_FETCH_MONTHS = MAX_WINDOW_MONTHS + 1  # small buffer above the max window

# Quadratic fit (on log-close) acceptance thresholds
MIN_R2_PRICE = 0.75
TROUGH_FRAC_MIN = 0.20            # trough must sit within the middle band
TROUGH_FRAC_MAX = 0.80            # of the window, not right at either edge
MAX_RESIDUAL_NOISE = 0.06         # std dev of log-price residuals from the fit
MIN_FORMATION_DAYS = 15           # trough must be >= ~3 trading weeks in the past

# OBV confirmation thresholds
MIN_OBV_SLOPE_R2 = 0.5            # looser than price fit -- OBV is noisier by nature
