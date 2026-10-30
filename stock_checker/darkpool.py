"""Dark pool score - off-exchange short share vs each stock's own normal.

FINRA publishes one file per trading day, each evening, with short volume
and total volume for every trade reported off-exchange (dark pools and
market makers). Free, no key:

    https://cdn.finra.org/equity/regsho/daily/CNMSshvolYYYYMMDD.txt

Most off-exchange short volume is market makers selling short to fill
buy orders, so a short share well ABOVE a stock's normal usually means
heavy hidden buying. The score reads it on a 1-10 scale:

    10  strong hidden buying, held for several days
     5  normal - most stocks, most days
     1  strong hidden selling

It is a confirmation check on a level you already like, not a signal on
its own: a day behind, and it never says who started a trade.

Fail-soft like the database layer: no network means no DP tags, and the
report runs as usual.
"""

import statistics
from datetime import date, timedelta

import requests

from .constants import FINRA_CACHE_DIR

FINRA_URL = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{}.txt"

LOOKBACK = 20            # trading days that define "normal"
MIN_HISTORY = 10         # fewer than this and there is no score
FETCH_WEEKDAYS = 35      # enough weekdays to cover LOOKBACK + streak + holidays
STREAK_DAYS = 3          # leaning the same way this many days earns a point
LEAN_Z = 0.75            # what counts as leaning
THIN_VOLUME = 0.7        # below this x normal volume, pull toward 5

# (z at or above, score) - checked top down. Below every row scores 1.
_SCORE_BANDS = [
    (2.5, 9), (2.0, 8), (1.5, 7), (0.75, 6),
    (-0.75, 5), (-1.5, 4), (-2.0, 3), (-2.5, 2),
]


def _fetch_day(d: date) -> str | None:
    """Return one day's file text, from cache or FINRA. None if no file.

    Weekends never have one. Holidays 403 - those get an empty marker once
    they are a few days old, so they are not re-requested every run. A
    recent miss is not marked: the file may just not be published yet.
    """
    key = d.strftime("%Y%m%d")
    path = FINRA_CACHE_DIR / f"{key}.txt"
    none_marker = FINRA_CACHE_DIR / f"{key}.none"
    if path.exists():
        return path.read_text(encoding="utf-8")
    if none_marker.exists():
        return None

    try:
        resp = requests.get(FINRA_URL.format(key), timeout=20)
    except requests.RequestException:
        return None
    if resp.status_code == 200 and resp.text.startswith("Date|"):
        path.write_text(resp.text, encoding="utf-8")
        return resp.text
    if (date.today() - d).days > 3:
        none_marker.touch()
    return None


def _prune(keep_from: date) -> None:
    """Drop cached files older than the window - about 0.5MB each."""
    cutoff = keep_from.strftime("%Y%m%d")
    for f in FINRA_CACHE_DIR.iterdir():
        if f.stem < cutoff:
            f.unlink(missing_ok=True)


def _finra_symbol(ticker: str) -> str:
    # yfinance writes share classes BRK-B, FINRA writes BRK/B
    return ticker.replace("-", "/")


def load_history(tickers: list) -> dict:
    """Fetch the window and return {ticker: [(date, short, total), ...]}.

    Oldest first. Only the requested tickers are kept - each file has
    every symbol in the market, about 12k lines.
    """
    FINRA_CACHE_DIR.mkdir(exist_ok=True)
    wanted = {_finra_symbol(t): t for t in tickers}
    history = {t: [] for t in tickers}

    today = date.today()
    days = []
    d = today
    while len(days) < FETCH_WEEKDAYS:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    _prune(days[0])

    for d in days:
        text = _fetch_day(d)
        if not text:
            continue
        for line in text.splitlines()[1:]:
            parts = line.split("|")
            if len(parts) < 5 or parts[1] not in wanted:
                continue
            try:
                short, total = float(parts[2]), float(parts[4])
            except ValueError:
                continue
            if total > 0:
                history[wanted[parts[1]]].append((d, short, total))
    return history


def _z_at(rows: list, i: int) -> float | None:
    """How unusual day i's short share is vs the LOOKBACK days before it."""
    prior = rows[max(0, i - LOOKBACK):i]
    if len(prior) < MIN_HISTORY:
        return None
    ratios = [s / t for _, s, t in prior]
    sd = statistics.stdev(ratios)
    if sd == 0:
        return None
    _, s, t = rows[i]
    return (s / t - statistics.mean(ratios)) / sd


def score(rows: list) -> dict | None:
    """Score the latest day in rows. None if there is not enough history."""
    if not rows:
        return None
    last = len(rows) - 1
    z = _z_at(rows, last)
    if z is None:
        return None

    pts = next((s for floor, s in _SCORE_BANDS if z >= floor), 1)

    # Streak: the latest STREAK_DAYS all leaning the same way.
    zs = [_z_at(rows, i) for i in range(last - STREAK_DAYS + 1, last + 1)]
    zs = [x for x in zs if x is not None]
    streak = 0
    if len(zs) == STREAK_DAYS:
        if all(x >= LEAN_Z for x in zs):
            streak = 1
        elif all(x <= -LEAN_Z for x in zs):
            streak = -1
    pts += streak

    # Thin volume: a big reading on a quiet day counts for less.
    prior_totals = [t for _, _, t in rows[max(0, last - LOOKBACK):last]]
    vol_x = rows[last][2] / statistics.mean(prior_totals)
    if vol_x < THIN_VOLUME and pts != 5:
        pts += -1 if pts > 5 else 1

    d, s, t = rows[last]
    return {
        "score": max(1, min(10, pts)),
        "z": round(z, 2),
        "ratio": round(s / t, 3),
        "avg_ratio": round(statistics.mean(
            s_ / t_ for _, s_, t_ in rows[max(0, last - LOOKBACK):last]), 3),
        "vol_x": round(vol_x, 2),
        "streak": streak,
        "as_of": d,
    }


def score_all(tickers: list) -> tuple[dict, dict]:
    """Return ({ticker: score dict}, history). Never raises."""
    try:
        history = load_history(tickers)
    except Exception as e:
        print(f"  [DP] dark pool data unavailable ({type(e).__name__}) - no DP tags")
        return {}, {}
    scores = {}
    for t, rows in history.items():
        try:
            s = score(rows)
        except Exception:
            s = None
        if s:
            scores[t] = s
    return scores, history
