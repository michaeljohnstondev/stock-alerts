from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from .constants import INTRADAY_VOLUME_PROFILE


def calculate_rsi(df: pd.DataFrame, period: int = 14) -> pd.Series:
    delta = df["Close"].diff()
    gain = delta.where(delta > 0, 0).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


def calculate_macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = df["Close"].ewm(span=fast, adjust=False).mean()
    ema_slow = df["Close"].ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_moving_averages(df: pd.DataFrame, short: int = 20, long: int = 50):
    return df["Close"].rolling(window=short).mean(), df["Close"].rolling(window=long).mean()


def calculate_bollinger_bands(df: pd.DataFrame, period: int = 20, std_dev: int = 2):
    sma = df["Close"].rolling(window=period).mean()
    std = df["Close"].rolling(window=period).std()
    return sma + (std * std_dev), sma, sma - (std * std_dev)


def calculate_200ma(df: pd.DataFrame) -> pd.Series:
    return df["Close"].rolling(window=200).mean()


def calculate_ema(df: pd.DataFrame, period: int) -> pd.Series:
    return df["Close"].ewm(span=period, adjust=False).mean()


def calculate_stochastic(df: pd.DataFrame, k_period: int = 14, d_period: int = 3):
    low_min = df["Low"].rolling(window=k_period).min()
    high_max = df["High"].rolling(window=k_period).max()
    k_line = 100 * (df["Close"] - low_min) / (high_max - low_min)
    d_line = k_line.rolling(window=d_period).mean()
    return k_line, d_line


def calculate_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    plus_di = 100 * (plus_dm.rolling(window=period).mean() / atr)
    minus_di = 100 * (minus_dm.rolling(window=period).mean() / atr)
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di))
    adx = dx.rolling(window=period).mean()
    return adx


def _intraday_volume_scale() -> float:
    """Scaling factor to project partial-day volume to full-day equivalent.
    Returns 1.0 outside market hours (no adjustment needed).
    During market hours, returns 1/cumulative_fraction based on U-shaped profile."""
    now_et = datetime.now(ZoneInfo("America/New_York"))

    if now_et.weekday() >= 5:  # weekend
        return 1.0

    now_mins = now_et.hour * 60 + now_et.minute
    market_open = 9 * 60 + 30   # 9:30 AM ET
    market_close = 16 * 60      # 4:00 PM ET

    if now_mins <= market_open or now_mins >= market_close:
        return 1.0

    elapsed = now_mins - market_open

    # Interpolate cumulative fraction from profile
    prev_mins, prev_frac = INTRADAY_VOLUME_PROFILE[0]
    cum_frac = 1.0
    for mins, frac in INTRADAY_VOLUME_PROFILE[1:]:
        if elapsed <= mins:
            span = mins - prev_mins
            t = (elapsed - prev_mins) / span if span > 0 else 1.0
            cum_frac = prev_frac + t * (frac - prev_frac)
            break
        prev_mins, prev_frac = mins, frac

    # Less than 10% of day's volume done = too noisy to project reliably
    if cum_frac < 0.10:
        return 1.0

    return 1.0 / cum_frac


def calculate_volume_ratio(df: pd.DataFrame, avg_period: int = 20) -> float:
    if len(df) < avg_period + 1:
        return 1.0
    avg_vol = df["Volume"].iloc[-(avg_period + 1):-1].mean()
    if avg_vol == 0:
        return 1.0
    today_vol = df["Volume"].iloc[-1]
    # During market hours, project partial-day volume to full-day equivalent
    today_vol *= _intraday_volume_scale()
    return today_vol / avg_vol


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Average True Range — used for stop placement and targets."""
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period).mean()


def find_swing_low(df: pd.DataFrame, lookback: int = 20) -> float:
    """Recent swing low — nearest support for stop placement."""
    return df["Low"].iloc[-lookback:].min()


def find_swing_high(df: pd.DataFrame, lookback: int = 20) -> float:
    """Recent swing high — nearest resistance for stop placement."""
    return df["High"].iloc[-lookback:].max()


def find_nearest_support(df: pd.DataFrame, price: float) -> float:
    """Find nearest support below price using recent pivot lows."""
    lows = df["Low"].iloc[-60:]
    # Look for local minima (lower than both neighbors)
    supports = []
    for i in range(2, len(lows) - 2):
        if (lows.iloc[i] < lows.iloc[i-1] and lows.iloc[i] < lows.iloc[i-2]
                and lows.iloc[i] < lows.iloc[i+1] and lows.iloc[i] < lows.iloc[i+2]):
            supports.append(lows.iloc[i])
    # Return nearest support below current price
    below = [s for s in supports if s < price]
    return max(below) if below else find_swing_low(df, 20)


def find_nearest_resistance(df: pd.DataFrame, price: float) -> float:
    """Find nearest resistance above price using recent pivot highs."""
    highs = df["High"].iloc[-60:]
    resistances = []
    for i in range(2, len(highs) - 2):
        if (highs.iloc[i] > highs.iloc[i-1] and highs.iloc[i] > highs.iloc[i-2]
                and highs.iloc[i] > highs.iloc[i+1] and highs.iloc[i] > highs.iloc[i+2]):
            resistances.append(highs.iloc[i])
    above = [r for r in resistances if r > price]
    return min(above) if above else find_swing_high(df, 20)


def calculate_fibonacci_levels(df: pd.DataFrame, lookback: int = 60) -> dict:
    """Calculate Fibonacci retracement levels from recent swing high/low."""
    recent = df.iloc[-lookback:]
    swing_high = recent["High"].max()
    swing_low = recent["Low"].min()
    diff = swing_high - swing_low
    return {
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "fib_236": round(swing_high - diff * 0.236, 2),
        "fib_382": round(swing_high - diff * 0.382, 2),
        "fib_500": round(swing_high - diff * 0.500, 2),
        "fib_618": round(swing_high - diff * 0.618, 2),
        "fib_786": round(swing_high - diff * 0.786, 2),
    }


def _find_swing_points(values, order: int = 5):
    """Find local maxima/minima using a rolling window of `order` bars each side."""
    points = []
    for i in range(order, len(values) - order):
        window = values[i - order: i + order + 1]
        if values[i] == max(window):
            points.append((i, float(values[i]), "high"))
        elif values[i] == min(window):
            points.append((i, float(values[i]), "low"))
    return points


def detect_vcp(df: pd.DataFrame) -> dict:
    """Detect Volatility Contraction Pattern using swing-point analysis.
    Finds successive pullback bases where each is shallower than the last,
    indicating supply drying up before a breakout.
    Requires: uptrend (price > 150 MA > 200 MA) + at least 2 contracting
    pullback bases + declining volume + price near pivot.
    Returns dict with state: 'none', 'forming', or 'breakout'."""
    if len(df) < 200:
        return {"state": "none"}

    price = df["Close"].iloc[-1]
    ma150 = df["Close"].rolling(150).mean().iloc[-1]
    ma200 = df["Close"].rolling(200).mean().iloc[-1]

    if pd.isna(ma150) or pd.isna(ma200):
        return {"state": "none"}

    # Minervini template: price > 150 MA > 200 MA (confirmed uptrend)
    if not (price > ma150 and ma150 > ma200):
        return {"state": "none"}

    # Find swing highs and lows over last 90 days
    lookback = min(90, len(df))
    segment = df.iloc[-lookback:]
    highs = segment["High"].values
    lows = segment["Low"].values

    swing_points = _find_swing_points(highs, order=5)
    swing_lows = _find_swing_points(lows, order=5)

    # Extract just the swing highs and swing lows
    sh_list = [(i, val) for i, val, kind in swing_points if kind == "high"]
    sl_list = [(i, val) for i, val, kind in swing_lows if kind == "low"]

    if len(sh_list) < 2 or len(sl_list) < 1:
        return {"state": "none"}

    # Build pullback bases: each swing high paired with its following swing low
    bases = []
    for sh_idx, sh_price in sh_list:
        next_lows = [(sl_idx, sl_price) for sl_idx, sl_price in sl_list
                     if sl_idx > sh_idx]
        if next_lows:
            sl_idx, sl_price = next_lows[0]
            depth = (sh_price - sl_price) / sh_price * 100
            if depth > 0:
                bases.append({"high": sh_price, "low": sl_price, "depth": depth})

    if len(bases) < 2:
        return {"state": "none"}

    # Find longest streak of tightening bases ending at the most recent base
    streak = 0
    for i in range(len(bases) - 1, 0, -1):
        if bases[i]["depth"] < bases[i - 1]["depth"]:
            streak += 1
        else:
            break

    if streak < 1:  # Need at least 2 bases (1 tightening step)
        return {"state": "none"}

    # Contracting bases = the ones in the streak
    contracting = bases[-(streak + 1):]
    n_contractions = len(contracting)
    tightest_depth = contracting[-1]["depth"]

    # Pivot = highest swing high across the contracting bases
    pivot = max(b["high"] for b in contracting)
    dist_to_pivot = max(0.0, (pivot - price) / price * 100)

    # Volume declining: recent 15-day avg < prior 30-day avg by at least 15%
    vol_recent = df["Volume"].iloc[-15:].mean()
    vol_prior = df["Volume"].iloc[-45:-15].mean()
    if vol_prior == 0:
        return {"state": "none"}
    if vol_recent >= vol_prior * 0.85:
        return {"state": "none"}

    # Breakout: price at or above pivot with volume surge
    today_vol = df["Volume"].iloc[-1]
    avg_vol_20 = df["Volume"].iloc[-21:-1].mean()
    vol_surge = today_vol / avg_vol_20 if avg_vol_20 > 0 else 1.0

    if price >= pivot * 0.998 and vol_surge >= 1.5:
        return {
            "state": "breakout",
            "pivot": round(pivot, 2),
            "contractions": n_contractions,
            "vol_surge": round(vol_surge, 1),
            "tightest_atr": round(tightest_depth, 2),
        }

    # Forming: declining volume + within 5% of pivot
    if dist_to_pivot <= 5.0:
        return {
            "state": "forming",
            "pivot": round(pivot, 2),
            "dist_pct": round(dist_to_pivot, 1),
            "contractions": n_contractions,
            "tightest_atr": round(tightest_depth, 2),
        }

    return {"state": "none"}
