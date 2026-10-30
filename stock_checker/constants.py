from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent

REPORTS_DIR = ROOT_DIR / "reports"
STATE_FILE = ROOT_DIR / ".regime_state.json"
SECTOR_STATE_FILE = ROOT_DIR / ".sector_state.json"
PICKS_HISTORY_FILE = ROOT_DIR / ".picks_history.json"
LAST_SCAN_FILE = ROOT_DIR / ".last_scan.json"
ADAPTIVE_STATE_FILE = ROOT_DIR / ".adaptive_state.json"
USER_PREFS_FILE = ROOT_DIR / ".user_preferences.json"
FAVORITES_FILE = ROOT_DIR / ".favorites.json"
ALERTS_FILE = ROOT_DIR / ".alerts.json"
PLAY_TRACKER_FILE = ROOT_DIR / ".play_tracker.json"
MISSED_MOVERS_FILE = ROOT_DIR / ".missed_movers_history.json"
USER_RATINGS_FILE = ROOT_DIR / ".user_ratings.json"
SKIPPED_FILE = ROOT_DIR / ".skipped.json"  # tickers snoozed until a date
FINRA_CACHE_DIR = ROOT_DIR / ".finra_cache"  # daily off-exchange short volume files
SKIP_DAYS_DEFAULT = 14  # let a big move settle before looking again

# How close a rung must be to show in APPROACHING, as a percentage.
# A 5-star is a specific price being waited on, so it gets a tighter window.
NEAR_PCT = 5.0
NEAR_PCT_BY_RATING = {5: 2.5}
WINNERS_JSON = ROOT_DIR / "winners.json"
LOSERS_JSON = ROOT_DIR / "losers.json"
ADAPTIVE_ALPHA = 0.25  # EMA smoothing — each run moves 25% toward new computed value
TRACK_DAYS = [1, 5, 10]  # Score picks at 1, 5, and 10 trading days
SCRATCH_THRESHOLD = 0.005  # 0.5% — expired trade needs at least this PnL to count as win (opportunity cost)

# Tier weighting for adaptive learning — higher-tier picks influence learning more
# A-tier = highest conviction, most informative outcomes (full vote)
# C-tier excluded entirely — watchlist only, not tracked or learned from
TIER_ADAPTIVE_WEIGHTS = {"A": 0.30, "B": 0.06}

# Signal weights — how much each indicator is worth
# Structure/demand signals weighted higher than pure oscillator readings
SIGNAL_WEIGHTS = {
    "RSI": 1.0,            # strong — but only fires at true extremes
    "MACD": 1.0,            # strong — real momentum shift
    "200 MA": 1.0,          # strong — major trend level
    "Support/Resist": 1.0,  # strong — structural level with price reaction
    "VCP Breakout": 1.0,    # strong — Minervini volatility contraction breakout
    "EMA21 Pullback": 0.85, # solid — trend continuation with demand
    "5 EMA Touch": 0.70,    # proximity to 5 EMA
    "200 MA Touch": 0.85,   # proximity to 200 MA — significant level
    "MA Cross": 0.75,       # moderate — lagging but confirmed
    "ADX Trend": 0.75,      # solid — confirms trend strength exists
    "Stochastic": 0.5,      # supporting — redundant with RSI
    "Bollinger": 0.5,       # weakest — can ride the band
    "Turnaround": 0.85,     # solid — beaten-down stock showing first signs of life
    "Post-Earnings": 1.0,   # strong — earnings risk resolved, gap level is structural
    "Breakout Retest": 1.0, # strong — structural confirmation of breakout
    "RSI Divergence": 0.85, # solid — price/RSI disagreement signals reversal
    "Gap Fill": 0.75,       # moderate — gap levels act as magnets
    "Volume Climax": 0.85,  # solid — capitulation volume often marks bottoms
    "Channel Bounce": 0.75, # moderate — range-bound mean reversion
    "Golden Cross": 1.0,    # strong — institutional 50/200 MA crossover
    "Failed Breakdown": 1.0,# strong — trap reversal, highest R:R (bullish)
    "Failed Breakout": 1.0, # strong — trap reversal, highest R:R (bearish)
    "Double Bottom": 0.85,  # solid — classic reversal with defined risk (bullish)
    "Double Top": 0.85,     # solid — classic reversal with defined risk (bearish)
    "Trend Accel": 0.85,    # solid — momentum continuation, MAs stacked
    "Mean Reversion": 0.75, # moderate — statistical snapback from extended levels
    "Accumulation": 0.85,  # solid — institutional footprint before breakout (2+ conditions)
    "Early Accumulation": 0.5,  # speculative — single early condition, adaptive learns if valuable
    "Distribution": 0.85,       # solid — institutional distribution (bearish counterpart of Accumulation)
    "Early Distribution": 0.5,  # speculative — single early condition (bearish counterpart)
    "Range Compression": 0.85,  # solid — coiled spring, ATR at 6mo low with triangle
    "Dist Breakdown": 0.85,     # solid — 3+ lower highs with distribution volume
    "Higher Lows": 0.85,        # solid — 3+ higher lows with accumulation volume
    "M on Uptrend": 1.0,        # strong — compound pattern: ascending peaks + ascending valleys + trendline entry
    "W on Downtrend": 1.0,      # strong — compound pattern: descending troughs + descending peaks + trendline entry
    "M on Downtrend": 1.0,       # strong — descending valleys projecting V3 dip entry
    "M Breakout Up": 1.0,        # strong — ascending M broke above P2 resistance with volume
    "M Breakout Down": 1.0,      # strong — descending M broke above P2, reversal confirmed with volume
    "Trendline Channel": 1.0,   # strong — compound pattern: 3+ peaks/valleys with trendline fit, entry at channel boundary
    "Trendline Break": 1.0,     # strong — breakout through descending resistance or ascending support trendline with volume
    "Deep Pullback": 0.85,      # solid — price extended below multiple key MAs, potential dip-buy setup
    "Earnings Beat Drift": 0.85,  # solid — beat earnings but stock hasn't caught up (PEAD)
    "Earnings Overreaction": 0.85,# solid — missed earnings but stock dropped way too much
    "Earnings Anticipation": 0.75,# moderate — oversold stock approaching earnings (pre-earnings run)
}

MAX_INDICATOR_SCORE = 4.0
MAX_TREND_SCORE = 3.0
MAX_VOLUME_SCORE = 2.0
MAX_CONVICTION_SCORE = 1.0

TIER_A_MIN = 7.5
TIER_B_MIN = 5.5
TIER_C_MIN = 3.5

# Confluence gate — 4 signals normally, but 3 elite signals can qualify
MIN_SIGNALS = 4
MIN_WEIGHT_3SIG = 2.5  # 3 signals need combined weight >= 2.5 (avg 0.83+)

# Sector ETFs for rotation analysis
SECTOR_ETFS = {
    "Technology": "XLK",
    "Financial Services": "XLF",
    "Healthcare": "XLV",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Consumer Defensive": "XLP",
    "Utilities": "XLU",
    "Communication Services": "XLC",
    "Consumer Cyclical": "XLY",
    "Basic Materials": "XLB",
    "Real Estate": "XLRE",
}

# Position value caps (% of account) — prevent absurd position sizes from tight stops
MAX_POSITION_PCT = {"FULL 1%": 0.25, "HALF 0.5%": 0.15, "QUARTER 0.25%": 0.10}

# Earnings proximity thresholds (trading days)
EARNINGS_SKIP_DAYS = 2    # Skip trade entirely — binary event
EARNINGS_WARN_DAYS = 5    # Force QUARTER + warning
EARNINGS_NOTE_DAYS = 10   # Informational note only

# Play type classification — every pick gets a named play
PLAY_TYPES = {
    "oversold_bounce":    "Oversold Bounce",
    "macd_momentum":      "MACD Momentum",
    "ema21_pullback":     "EMA21 Pullback",
    "5_ema_touch":        "5 EMA Touch",
    "200_ma_touch":       "200 MA Touch",
    "vcp_breakout":       "VCP Breakout",
    "ma200_reclaim":      "200 MA Reclaim",
    "ma200_loss":         "200 MA Loss",
    "support_bounce":     "Support Bounce",
    "resistance_reject":  "Resistance Rejection",
    "turnaround":         "Turnaround Recovery",
    "sector_ride":        "Sector Rotation Ride",
    "post_earnings":      "Post-Earnings Pullback",
    "post_earnings_bear": "Post-Earnings Fade",
    "breakout_retest":    "Breakout Retest",
    "rsi_divergence":     "RSI Divergence",
    "gap_fill":           "Gap Fill",
    "volume_climax":      "Volume Climax",
    "channel_bounce":     "Channel Bounce",
    "golden_cross":       "Golden Cross",
    "death_cross":        "Death Cross",
    "failed_breakdown":   "Failed Breakdown",
    "failed_breakout":    "Failed Breakout",
    "double_bottom":      "Double Bottom",
    "double_top":         "Double Top",
    "trend_accel":        "Trend Acceleration",
    "mean_reversion":     "Mean Reversion Extreme",
    "rs_continuation":    "RS Continuation",
    "accumulation":       "Accumulation Setup",
    "distribution":       "Distribution Setup",
    "early_accumulation": "Early Accumulation",
    "early_distribution": "Early Distribution",
    "range_compression":  "Range Compression Breakout",
    "lower_highs_dist":   "Lower Highs Breakdown",
    "higher_lows":        "Higher Lows Breakout",
    "m_on_uptrend":       "M on Uptrend",
    "w_on_downtrend":     "W on Downtrend",
    "m_on_downtrend":     "M on Downtrend",
    "m_breakout_up":      "M Breakout Up",
    "m_breakout_down":    "M Breakout Down",
    "channel_support":    "Channel Support Bounce",
    "channel_resist":     "Channel Resistance Short",
    "descending_trendline_breakout": "Descending Trendline Breakout",
    "ascending_trendline_breakdown": "Ascending Trendline Breakdown",
    "deep_pullback":      "Deep Pullback",
    "earnings_beat_drift":    "Earnings Beat Drift",
    "earnings_overreaction":  "Earnings Overreaction",
    "earnings_anticipation":  "Earnings Anticipation",
    "multi_signal":       "Multi-Signal Confluence",
}

# Per-play configuration — controls which plays are active and their parameters
# enabled: show in report + track as picks (signals/scoring still run for all)
# vol_floor: minimum volume score in calculate_score() (overrides score_volume() floor)
# vol_gate: minimum vol_ratio before hard rejection in chop regime (default 0.7)
PLAY_CONFIG = {
    # --- BATCH 1: Reversal plays (enabled) ---
    "support_bounce":   {"enabled": True,  "vol_gate": 0.5, "vol_floor": 0.25},
    "failed_breakdown": {"enabled": True,  "vol_gate": 0.5, "vol_floor": 0.25},
    "double_bottom":    {"enabled": True,  "vol_gate": 0.5, "vol_floor": 0.25},
    "volume_climax":    {"enabled": True,  "vol_gate": 0.5, "vol_floor": 0.25},
    # --- Everything else: disabled until validated ---
    # Flip enabled to True when ready to add a new batch
}
PLAY_CONFIG_DEFAULTS = {"enabled": False, "vol_gate": 0.7, "vol_floor": 0.0}


def get_play_config(play_type: str) -> dict:
    """Get merged config for a play type (specific overrides + defaults)."""
    return {**PLAY_CONFIG_DEFAULTS, **PLAY_CONFIG.get(play_type, {})}


# Key signals that define each play type — used in report section headers
PLAY_TYPE_KEY_SIGNALS = {
    "oversold_bounce":    ["RSI", "Support/Resist", "Stochastic"],
    "macd_momentum":      ["MACD", "ADX Trend"],
    "ema21_pullback":     ["EMA21 Pullback", "ADX Trend"],
    "5_ema_touch":        ["5 EMA Touch"],
    "200_ma_touch":       ["200 MA Touch"],
    "vcp_breakout":       ["VCP Breakout", "Range Compression"],
    "ma200_reclaim":      ["200 MA", "MACD"],
    "ma200_loss":         ["200 MA", "MACD"],
    "support_bounce":     ["Support/Resist", "RSI"],
    "resistance_reject":  ["Support/Resist", "RSI"],
    "turnaround":         ["Turnaround", "Volume Climax"],
    "sector_ride":        ["Accumulation", "Higher Lows", "Trend Accel"],
    "post_earnings":      ["Post-Earnings", "Gap Fill"],
    "post_earnings_bear": ["Post-Earnings", "Gap Fill"],
    "breakout_retest":    ["Breakout Retest", "Support/Resist", "Higher Lows"],
    "rsi_divergence":     ["RSI Divergence", "Support/Resist"],
    "gap_fill":           ["Gap Fill"],
    "volume_climax":      ["Volume Climax", "RSI"],
    "channel_bounce":     ["Channel Bounce", "Support/Resist"],
    "golden_cross":       ["Golden Cross", "200 MA"],
    "death_cross":        ["Golden Cross", "200 MA"],
    "failed_breakdown":   ["Failed Breakdown", "Volume Climax"],
    "failed_breakout":    ["Failed Breakout", "Volume Climax"],
    "double_bottom":      ["Double Bottom", "RSI Divergence"],
    "double_top":         ["Double Top", "RSI Divergence"],
    "trend_accel":        ["Trend Accel", "ADX Trend", "EMA21 Pullback"],
    "mean_reversion":     ["Mean Reversion", "RSI", "Bollinger"],
    "rs_continuation":    ["Trend Accel", "Higher Lows"],
    "accumulation":       ["Accumulation", "Higher Lows", "ADX Trend"],
    "distribution":       ["Distribution", "Dist Breakdown"],
    "early_accumulation": ["Early Accumulation", "Higher Lows"],
    "early_distribution": ["Early Distribution", "Dist Breakdown"],
    "range_compression":  ["Range Compression", "VCP Breakout"],
    "lower_highs_dist":   ["Dist Breakdown", "Distribution"],
    "higher_lows":        ["Higher Lows", "Accumulation"],
    "m_on_uptrend":       ["M on Uptrend", "Trendline Channel"],
    "w_on_downtrend":     ["W on Downtrend", "Trendline Channel"],
    "m_on_downtrend":     ["M on Downtrend"],
    "m_breakout_up":      ["M Breakout Up"],
    "m_breakout_down":    ["M Breakout Down"],
    "channel_support":    ["Trendline Channel", "Support/Resist"],
    "channel_resist":     ["Trendline Channel", "Support/Resist"],
    "descending_trendline_breakout": ["Trendline Break", "Volume Climax"],
    "ascending_trendline_breakdown": ["Trendline Break", "Distribution"],
    "deep_pullback":      ["Deep Pullback", "Support/Resist"],
    "earnings_beat_drift":    ["Earnings Beat Drift"],
    "earnings_overreaction":  ["Earnings Overreaction"],
    "earnings_anticipation":  ["Earnings Anticipation", "RSI"],
    "multi_signal":       ["Multiple Signals"],
}

# Approaching setup detection thresholds (ATR-normalized)
APPROACHING_THRESHOLDS = {
    "ema_convergence_pct": 0.03,       # within 3% of EMA
    "ema_convergence_atr": 1.5,        # or within 1.5 ATR
    "support_resist_pct": 0.025,       # within 2.5% of S/R level
    "support_resist_atr": 1.0,         # or within 1.0 ATR
    "channel_boundary_atr": 1.5,       # within 1.5 ATR of trendline
    "double_bottom_spread_pct": 0.04,  # second test within 4% of first low
}
APPROACHING_MIN_DISTANCE_PCT = 0.005   # must be >0.5% away (not already triggering)
APPROACHING_MAX_PER_PLAY = 5           # max approaching items per play section

# Approaching type → most likely play type mapping
APPROACHING_PLAY_MAP = {
    "ema21_approach":       "ema21_pullback",
    "ma200_approach":       "ma200_reclaim",
    "support_approach":     "support_bounce",
    "resist_approach":      "resistance_reject",
    "retest_approach":      "breakout_retest",
    "dbl_bottom_forming":   "double_bottom",
    "channel_sup_approach": "channel_support",
    "channel_res_approach": "channel_resist",
}

REGIME_CHOP_CAP = 9.0  # Max score in chop regime (less aggressive — adaptive will learn the right level)
MKT_DISAGREE_CAP = 6.5  # Score cap when trade direction opposes SPY/QQQ (prevents high-tier shorts in bull markets)
CHOP_VOL_MIN = 1.3  # Minimum volume ratio to trade in chop regime — need conviction
CHOP_TREND_VOL_MIN = 1.8  # Trend-continuation plays in chop need stronger volume proof
CHOP_TREND_PLAY_DISCOUNT = 0.80  # Score haircut for trend plays in chop (no trend to continue)
ADVERSE_STACK_PENALTY = 0.04  # Extra win rate penalty per adverse condition beyond the first

# Range-play regime awareness — in chop, range plays are the RIGHT playbook
RANGE_PLAY_SIGNALS = {
    "Channel Bounce", "Mean Reversion", "Bollinger", "Support/Resist",
    "Range Compression", "Double Bottom", "Double Top",
    "Failed Breakdown", "Failed Breakout", "Deep Pullback",
}
CHOP_TREND_DISCOUNT = 0.85   # Counter-trend discount in chop (vs 0.5 in trend)
CHOP_RANGE_BOOST = 0.75      # Score bonus for range plays in chop (applied before compression)
CHOP_TREND_GATE_MIN = 1      # Softer trend gate in chop (vs 2 in trend)

# Setup context for trend scoring — counter-trend plays get discounted trend credit
# Counter-trend: reversal/mean-reversion setups betting on a turn
COUNTER_TREND_SIGNALS = {
    "RSI", "Bollinger", "Stochastic", "Mean Reversion", "RSI Divergence",
    "Volume Climax", "Failed Breakdown", "Failed Breakout",
    "Double Bottom", "Double Top", "Turnaround", "Channel Bounce",
    "Deep Pullback",
}
# With-trend: continuation setups riding the existing trend
WITH_TREND_SIGNALS = {
    "EMA21 Pullback", "Trend Accel", "Golden Cross", "VCP Breakout",
    "Breakout Retest", "Accumulation", "Distribution", "Range Compression",
    "Dist Breakdown", "Higher Lows", "MA Cross",
    "M on Uptrend", "W on Downtrend",
}
# Neutral signals (don't determine context): 200 MA, Support/Resist, MACD,
# ADX Trend, Post-Earnings, Gap Fill, Early Accumulation, Early Distribution,
# Trendline Channel (range play — not fighting or riding a trend)

TREND_DISCOUNT_COUNTER = 0.5  # Counter-trend plays: trend_score *= 0.5

TREND_GATE_MIN = 2  # Need at least 2 of 4 conditions to avoid score cap
TREND_GATE_CAP = 5.5  # Score cap when trend gate fails

# EV (Expected Value) gates — centralized thresholds
EV_EDGE_MIN = 0.10      # Minimum EV to count as "has edge" in report (was 0.20)
EV_TIER_C_MAX = 0.05    # EV below this = forced C-tier (was 0.10)
EV_TIER_A_MIN = 0.15    # EV below this = can't be A-tier (was 0.20)
EV_SIZE_MIN = 0.10      # EV below this = position capped at QUARTER (was 0.20)
EV_SIZE_WIN_MIN = 50.0  # Win% below this + low EV = position capped (was 52%)

# Default pillar weights (max contribution to total score out of 10)
PILLAR_DEFAULTS = {
    "indicators": MAX_INDICATOR_SCORE,   # 4.0
    "trend":      MAX_TREND_SCORE,       # 3.0
    "volume":     MAX_VOLUME_SCORE,      # 2.0
    "conviction": MAX_CONVICTION_SCORE,  # 1.0
}

# Sector cache for when yfinance fails
SECTOR_CACHE_FILE = ROOT_DIR / ".sector_cache.json"

# Fundamental data cache (24h expiry — fundamentals don't change intraday)
FUNDAMENTAL_CACHE_FILE = ROOT_DIR / ".fundamental_cache.json"

# Value grade thresholds (0-100 score → letter grade)
# Direction-aware: longs reward good fundamentals, shorts reward bad fundamentals
VALUE_GRADES = [
    (90, "A+"), (80, "A"), (70, "B+"), (60, "B"),
    (50, "C+"), (40, "C"), (25, "D"), (0, "F"),
]

# Intraday cumulative volume profile — what fraction of daily volume is
# typically done by each point in the trading day.  Based on the well-documented
# U-shaped curve: heavy at open (institutional orders), quiet midday, heavy at
# close (MOC orders).  Used to project partial-day volume to full-day equivalent
# when scanning during market hours.
# Format: (minutes since 9:30 ET open, cumulative fraction of daily volume)
INTRADAY_VOLUME_PROFILE = (
    (0,   0.00),
    (30,  0.18),   # first 30 min = ~18% (opening rush)
    (60,  0.28),   # by 10:30
    (90,  0.35),   # by 11:00
    (120, 0.42),   # by 11:30
    (150, 0.48),   # by noon
    (180, 0.54),   # by 12:30
    (210, 0.60),   # by 1:00
    (240, 0.66),   # by 1:30
    (270, 0.72),   # by 2:00
    (300, 0.78),   # by 2:30
    (330, 0.84),   # by 3:00
    (360, 0.92),   # by 3:30 (closing rush begins)
    (390, 1.00),   # 4:00 PM close
)

# Market context ETFs — broader macro picture (informational, not scored)
MARKET_CONTEXT = {
    "TLT":     "Bonds (20Y Treasury)",
    "GLD":     "Gold",
    "BTC-USD": "Bitcoin",
}

# Signal learning dampening — volatile signals learn at reduced rate
SIGNAL_DAMPENING = {
    "MACD": 0.5,  # half learning rate — history shows wild swings on small samples
}

# Factor groups — signals measuring the same underlying phenomenon
# scored as ONE factor (max of member strengths, not sum)
SIGNAL_FACTOR_GROUPS = {
    "momentum":    ["RSI", "Stochastic", "MACD", "RSI Divergence"],
    "trend_ma":    ["200 MA", "EMA21 Pullback", "5 EMA Touch", "200 MA Touch", "MA Cross", "Golden Cross", "Trend Accel"],
    "structure":   ["Support/Resist", "Channel Bounce", "Double Bottom", "Double Top",
                    "Trendline Channel", "Trendline Break"],
    "volume_flow": ["Volume Climax", "Accumulation", "Early Accumulation",
                    "Distribution", "Early Distribution", "Higher Lows", "Dist Breakdown"],
    "breakout":    ["VCP Breakout", "Breakout Retest", "Range Compression",
                    "Failed Breakdown", "Failed Breakout"],
    "context":     ["ADX Trend", "Bollinger", "Post-Earnings", "Gap Fill",
                    "Turnaround", "Mean Reversion", "Deep Pullback",
                    "Earnings Beat Drift", "Earnings Overreaction", "Earnings Anticipation"],
    "compound":    ["M on Uptrend", "W on Downtrend", "M on Downtrend", "M Breakout Up", "M Breakout Down"],
}

FACTOR_WEIGHTS = {
    "momentum": 1.0, "trend_ma": 1.0, "structure": 1.0,
    "volume_flow": 0.85, "breakout": 1.0, "context": 0.75, "compound": 1.0,
}

# Reverse lookup: signal name -> factor name
SIGNAL_TO_FACTOR = {}
for _f, _sigs in SIGNAL_FACTOR_GROUPS.items():
    for _s in _sigs:
        SIGNAL_TO_FACTOR[_s] = _f

# Default sigmoid parameters for win rate estimation (cold start / fallback)
# Calibrated from data in get_adaptive_win_rates() after enough history
# Sector concentration caps — prevent over-exposure to one sector+direction
SECTOR_CONCENTRATION_MAX = 3  # max picks at original sizing per sector+direction

DEFAULT_SIGMOID_PARAMS = {
    "midpoint": 6.0,    # score where win rate crosses halfway
    "slope": 0.9,       # steepness of the curve
    "floor": 0.20,      # win rate at very low scores
    "ceiling": 0.75,    # win rate at very high scores
}

# User preference learning
PREF_MAX_BOOST = 0.05     # +5% max win rate boost from preferences
PREF_MAX_PENALTY = 0.02   # -2% max penalty (asymmetric — don't punish misalignment too hard)
PREF_HALF_LIFE_DAYS = 45  # Time decay half-life for preference selections
PREF_DIMENSION_WEIGHTS = {
    "play_type": 0.30,
    "signals": 0.25,
    "setup_context": 0.20,
    "sector_status": 0.15,
    "direction": 0.10,
}

# Conflicting signal penalty — opposing signals represent real headwinds
# even when discarded by the 60% contradiction resolution rule.
# Scales with opposing_weight / direction_weight (0.0 to 1.0)
CONFLICTING_SIGNAL_MAX_PENALTY = 1.0

# Overbought/extended penalty for with-trend momentum entries
# Does NOT apply to counter-trend setups (they bet on the reversal)
OVERBOUGHT_EXTENDED_PENALTY = 0.5   # both RSI overbought + statistically extended
OVERBOUGHT_ONLY_PENALTY = 0.3       # just RSI >= 70
EXTENDED_ONLY_PENALTY = 0.25         # just z-score >= 2.0 above MA50

# 52-week high proximity + overbought = chasing into exhaustion
HIGH_52W_OVERBOUGHT_WR_PENALTY = 0.05  # 5% win rate penalty

# Rotation divergence override — when the system detects internal market breakdown
# (defensive rotation in bull market, or offensive rotation in bear market),
# loosen caps for trades aligned with the divergence direction.
DIVERGENCE_MKT_CAP = 8.0           # raise MKT_DISAGREE_CAP from 6.5 to 8.0
DIVERGENCE_MKT_WR_PENALTY = 0.05   # reduce mkt_aligned penalty from 0.15 to 0.05

# ---------------------------------------------------------------------------
# Signal Leaderboards — per-signal top-10 sections for QA/validation
# ---------------------------------------------------------------------------

# Display config: (indicator, direction) -> (title, description)
# Synthetic keys start with underscore
LEADERBOARD_DISPLAY = {
    # --- Synthetic (all stocks, not signal-dependent) ---
    ("_volume", ""):               ("VOLUME", "Highest volume relative to 20-day average"),
    ("_support_proximity", ""):    ("SUPPORT PROXIMITY", "Stocks closest to key support levels"),
    ("_resistance_proximity", ""): ("RESISTANCE PROXIMITY", "Stocks closest to key resistance levels"),
    ("_200ma_proximity", ""):      ("200 MA PROXIMITY", "Stocks closest to the 200-day moving average"),
    ("_ema21_proximity", ""):      ("EMA21 PROXIMITY", "Stocks closest to the 21-day EMA"),
    # --- Momentum ---
    ("RSI", "bullish"):            ("RSI OVERSOLD", "Stocks with RSI in or near oversold territory"),
    ("RSI", "bearish"):            ("RSI OVERBOUGHT", "Stocks with RSI in or near overbought territory"),
    ("MACD", "bullish"):           ("MACD BULLISH CROSSOVER", "Stocks with recent MACD bullish crossovers"),
    ("MACD", "bearish"):           ("MACD BEARISH CROSSOVER", "Stocks with recent MACD bearish crossovers"),
    ("RSI Divergence", "bullish"): ("RSI DIVERGENCE (BULLISH)", "Price making new lows but RSI rising"),
    ("RSI Divergence", "bearish"): ("RSI DIVERGENCE (BEARISH)", "Price making new highs but RSI falling"),
    ("Stochastic", "bullish"):     ("STOCHASTIC OVERSOLD", "Stochastic K crossed above D in oversold zone"),
    ("Stochastic", "bearish"):     ("STOCHASTIC OVERBOUGHT", "Stochastic K crossed below D in overbought zone"),
    # --- Trend / MA ---
    ("200 MA", "bullish"):         ("ABOVE 200 MA", "Stocks reclaiming or holding above 200-day MA"),
    ("200 MA", "bearish"):         ("BELOW 200 MA", "Stocks losing or trading below 200-day MA"),
    ("EMA21 Pullback", "bullish"): ("EMA21 PULLBACK (BULL)", "Pullback to EMA21 with bounce confirmed"),
    ("5 EMA Touch", "bullish"):     ("5 EMA TOUCH", "Price near the 5 EMA"),
    ("200 MA Touch", "bullish"):    ("200 MA TOUCH", "Price near the 200 MA — key level"),
    ("EMA21 Pullback", "bearish"): ("EMA21 PULLBACK (BEAR)", "Bounce to EMA21 with rejection confirmed"),
    ("MA Cross", "bullish"):       ("MA CROSS BULLISH", "SMA20 crossed above SMA50"),
    ("MA Cross", "bearish"):       ("MA CROSS BEARISH", "SMA20 crossed below SMA50"),
    ("Golden Cross", "bullish"):   ("GOLDEN CROSS", "SMA50 crossed above SMA200"),
    ("Golden Cross", "bearish"):   ("DEATH CROSS", "SMA50 crossed below SMA200"),
    ("Trend Accel", "bullish"):    ("TREND ACCELERATION (BULL)", "Moving averages stacking bullish with acceleration"),
    ("Trend Accel", "bearish"):    ("TREND ACCELERATION (BEAR)", "Moving averages stacking bearish with acceleration"),
    ("ADX Trend", "bullish"):      ("ADX STRONG TREND (BULL)", "ADX > 25 confirming bullish directional strength"),
    ("ADX Trend", "bearish"):      ("ADX STRONG TREND (BEAR)", "ADX > 25 confirming bearish directional strength"),
    # --- Structure ---
    ("Support/Resist", "bullish"): ("SUPPORT BOUNCE", "Stocks bouncing off key support levels"),
    ("Support/Resist", "bearish"): ("RESISTANCE REJECTION", "Stocks rejected at key resistance levels"),
    ("Bollinger", "bullish"):      ("BOLLINGER LOWER BAND", "Price touching or piercing lower Bollinger Band"),
    ("Bollinger", "bearish"):      ("BOLLINGER UPPER BAND", "Price touching or piercing upper Bollinger Band"),
    ("Double Bottom", "bullish"):  ("DOUBLE BOTTOM", "Two matching lows with neckline forming"),
    ("Double Top", "bearish"):     ("DOUBLE TOP", "Two matching highs with neckline forming"),
    ("Trendline Channel", "bullish"):  ("TRENDLINE CHANNEL (BULL)", "Ascending trendline channel detected"),
    ("Trendline Channel", "bearish"):  ("TRENDLINE CHANNEL (BEAR)", "Descending trendline channel detected"),
    ("Trendline Break", "bullish"):    ("TRENDLINE BREAKOUT", "Price breaking above descending trendline"),
    ("Trendline Break", "bearish"):    ("TRENDLINE BREAKDOWN", "Price breaking below ascending trendline"),
    ("Channel Bounce", "bullish"):     ("CHANNEL SUPPORT BOUNCE", "Price bouncing off channel support"),
    ("Channel Bounce", "bearish"):     ("CHANNEL RESISTANCE REJECT", "Price rejected at channel resistance"),
    # --- Volume / Flow ---
    ("Volume Climax", "bullish"):  ("VOLUME CLIMAX (BULL)", "Extreme volume spike with bullish move"),
    ("Volume Climax", "bearish"):  ("VOLUME CLIMAX (BEAR)", "Extreme volume spike with bearish move"),
    ("Accumulation", "bullish"):   ("ACCUMULATION", "Institutional buying detected (OBV, volume ratio, basing)"),
    ("Distribution", "bearish"):   ("DISTRIBUTION", "Institutional selling detected"),
    ("Early Accumulation", "bullish"): ("EARLY ACCUMULATION", "Early signs of institutional buying (1/3 conditions)"),
    ("Early Distribution", "bearish"): ("EARLY DISTRIBUTION", "Early signs of institutional selling (1/3 conditions)"),
    ("Higher Lows", "bullish"):    ("HIGHER LOWS", "3+ ascending swing lows with buy volume dominant"),
    ("Dist Breakdown", "bearish"): ("LOWER HIGHS", "3+ descending swing highs with sell volume dominant"),
    # --- Breakout ---
    ("VCP Breakout", "bullish"):   ("VCP BREAKOUT", "Volatility contraction pattern breakout"),
    ("Range Compression", "bullish"):  ("RANGE COMPRESSION", "ATR at 6-month low — coiled spring"),
    ("Breakout Retest", "bullish"):    ("BREAKOUT RETEST", "Breakout level retested with hold"),
    ("Failed Breakdown", "bullish"):   ("FAILED BREAKDOWN", "Broke support then reversed — seller trap"),
    ("Failed Breakout", "bearish"):    ("FAILED BREAKOUT", "Broke resistance then reversed — buyer trap"),
    # --- Context ---
    ("Turnaround", "bullish"):     ("TURNAROUND", "Beaten-down stock showing first recovery signs"),
    ("Post-Earnings", "bullish"):  ("POST-EARNINGS (BULL)", "Bullish post-earnings gap setup"),
    ("Post-Earnings", "bearish"):  ("POST-EARNINGS (BEAR)", "Bearish post-earnings gap setup"),
    ("Gap Fill", "bullish"):       ("GAP FILL (BULL)", "Prior gap level acting as upside magnet"),
    ("Gap Fill", "bearish"):       ("GAP FILL (BEAR)", "Prior gap level acting as downside magnet"),
    ("Mean Reversion", "bullish"): ("MEAN REVERSION (BULL)", "Price statistically extended below moving averages"),
    ("Mean Reversion", "bearish"): ("MEAN REVERSION (BEAR)", "Price statistically extended above moving averages"),
    ("Deep Pullback", "bullish"):  ("DEEP PULLBACK", "Extended dip below multiple key MAs"),
    # --- Compound ---
    ("M on Uptrend", "bullish"):   ("M ON UPTREND", "Ascending M-pattern at trendline support"),
    ("W on Downtrend", "bearish"): ("W ON DOWNTREND", "Descending W-pattern at trendline resistance"),
    ("M on Downtrend", "bullish"):  ("M ON DOWNTREND", "Descending valleys projecting V3 — predictive dip entry"),
    ("M Breakout Up", "bullish"):   ("M BREAKOUT UP", "Ascending M broke resistance with volume"),
    ("M Breakout Down", "bullish"): ("M BREAKOUT DOWN", "Descending M broke resistance — reversal confirmed"),
    # --- Earnings ---
    ("Earnings Beat Drift", "bullish"):    ("EARNINGS BEAT DRIFT", "Beat earnings but stock hasn't caught up — PEAD opportunity"),
    ("Earnings Overreaction", "bullish"):  ("EARNINGS OVERREACTION", "Missed earnings but stock dropped disproportionately — oversold bounce"),
    ("Earnings Anticipation", "bullish"):  ("EARNINGS ANTICIPATION", "Oversold stock approaching earnings — pre-earnings run potential"),
}

# Display order — bullish signals only (long-only trading)
LEADERBOARD_ORDER = [
    # Synthetic proximity/volume
    ("_volume", ""),
    ("_support_proximity", ""),
    ("_200ma_proximity", ""),
    ("_ema21_proximity", ""),
    # Momentum
    ("RSI", "bullish"),
    ("MACD", "bullish"),
    ("Stochastic", "bullish"),
    ("RSI Divergence", "bullish"),
    # Trend / MA
    ("200 MA", "bullish"),
    ("EMA21 Pullback", "bullish"),
    ("5 EMA Touch", "bullish"),
    ("200 MA Touch", "bullish"),
    ("MA Cross", "bullish"),
    ("Golden Cross", "bullish"),
    ("Trend Accel", "bullish"),
    ("ADX Trend", "bullish"),
    # Structure
    ("Support/Resist", "bullish"),
    ("Bollinger", "bullish"),
    ("Double Bottom", "bullish"),
    ("Trendline Channel", "bullish"),
    ("Trendline Break", "bullish"),
    ("Channel Bounce", "bullish"),
    # Volume / Flow
    ("Volume Climax", "bullish"),
    ("Accumulation", "bullish"),
    ("Early Accumulation", "bullish"),
    ("Higher Lows", "bullish"),
    # Breakout
    ("VCP Breakout", "bullish"),
    ("Range Compression", "bullish"),
    ("Breakout Retest", "bullish"),
    ("Failed Breakdown", "bullish"),
    # Context
    ("Turnaround", "bullish"),
    ("Post-Earnings", "bullish"),
    ("Gap Fill", "bullish"),
    ("Mean Reversion", "bullish"),
    ("Deep Pullback", "bullish"),
    ("M on Uptrend", "bullish"),
    ("M on Downtrend", "bullish"),
    ("M Breakout Up", "bullish"),
    ("M Breakout Down", "bullish"),
    # Earnings
    ("Earnings Beat Drift", "bullish"),
    ("Earnings Overreaction", "bullish"),
    ("Earnings Anticipation", "bullish"),
]
