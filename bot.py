"""
NAS100 Scalper bot  -  MetaApi (MT5) + GitHub Actions + phone dashboard.

What it does during the New York session (all times New York):
  09:15  connects to MT5 and starts building 1-minute candles
  09:35  Opening-range trade: direction of the first 5-minute candle,
         stop on the far side of that candle, stacked take-profits 2R..10R
  09:40-14:45  Pullback scalps on 1-minute candles:
         trend = EMA9 vs EMA21, entry when price dips to EMA9 and closes back
         in the trend direction, stop under/over the last 3 candles,
         stacked take-profits 1R..2.5R
  15:00  closes everything and disconnects

Every setup is opened as a STACK of positions (default 10) that SHARE one risk
budget (default 0.5% of balance), so ten positions risk the same as one.
After half the stack has hit take-profit, the rest move to breakeven.
Daily loss limit (default 2%) stops trading for the day.

Start/Stop comes from control.json in the repo (the phone app edits it).
Live status for the app is written to status.json on the "status" branch.

Usage:
  python bot.py run      # trading session (scheduled, or started from the app)
  python bot.py close    # close every bot position now
  python bot.py status   # print account + symbol info
"""

import asyncio
import base64
import json
import math
import os
import sys
import urllib.error
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")
TAG = "NASbot"


def now_ny():
    return datetime.now(NY)


# ================================================================ settings
def env(name, default=None):
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


@dataclass
class Settings:
    token: str = None
    account_id: str = None
    symbol: str = "USTEC"            # Exness NAS100 (Standard accounts: USTECm)
    risk_percent: float = 0.5        # % of balance per setup (whole stack together)
    stack_size: int = 10             # positions opened per setup
    daily_loss_percent: float = 2.0  # stop for the day after this drawdown
    max_setups: int = 8              # max setups per day
    max_lot: float = 2.0             # cap on total volume of one stack
    slippage_points: float = 1.0
    max_stop_points: float = 60.0    # skip scalps needing a wider stop
    allow_real: bool = False
    run_source: str = "schedule"     # "schedule" or "manual"
    # ---- aggressive scalping mode (STYLE=aggressive)
    style: str = "standard"          # "standard" or "aggressive"
    target_points: float = 5.0       # close at +X points of real profit (after spread)
    emergency_stop_points: float = 50.0
    max_trades: int = 60             # max aggressive trades per day
    spread_mult: float = 1.5         # skip entries when spread > 1.5x its recent median
    loss_pause_minutes: int = 15     # pause after 2 emergency stops in a row
    signal_tf: int = 5               # aggressive entries on 1- or 5-minute candles
    # ---- trend mode (STYLE=trend)
    trend_band: float = 1.0          # noise-area width multiplier
    trend_lookback: int = 14         # days used to measure normal noise
    # ---- stack & compound mode (STYLE=stack)
    stack_count: int = 8             # positions opened together per signal
    lot_per_1000: float = 0.1        # lot PER POSITION for every $1,000 of balance (compounds)
    basket_tp_percent: float = 1.5   # close the whole basket at +X% of balance
    basket_sl_percent: float = 10.0  # close the whole basket at -X% of balance
    stack_tf: int = 1                # entries on 1-minute candles
    stack_counts: str = "5,8,10"     # positions per basket: grows after each winning basket
    stack_exit: str = "position"     # "position": each position closes alone at +X pts profit
    position_tp_points: float = 10.0 # profit target of EACH position (on the Exness server)
    stack_breakeven: bool = True     # after the first position wins, move the others' stop to entry
    stack_tp_step: float = 3.0       # targets step up: +10, +13, +16 ... so positions close one by one
    fast_entry: bool = True          # enter instantly on a fast burst in the trend direction
    burst_k: float = 0.6             # burst = price moved >= 0.6 x average 1-min range within 10 s
    adaptive_targets: bool = True    # targets follow market speed (ATR) instead of fixed points
    streaming: bool = True           # live price stream from MetaApi (falls back to polling)
    # ---- intelligence
    regime_filter: bool = True       # trend / mixed / chop detection (efficiency ratio)
    mtf_confirm: bool = True         # 5-min and 15-min trends must agree with the entry
    self_learning: bool = True       # skip conditions that keep losing (learned from own results)
    trailing_lock: bool = True       # after breakeven, trail the stop behind the best price
    stack_trend_filter: bool = True  # only open baskets when price is outside the noise area

    @classmethod
    def load(cls):
        s = cls(
            token=env("METAAPI_TOKEN"),
            account_id=env("METAAPI_ACCOUNT_ID"),
            symbol=env("SYMBOL", "USTEC"),
            risk_percent=float(env("RISK_PERCENT", "0.5")),
            stack_size=int(env("STACK_SIZE", "10")),
            daily_loss_percent=float(env("DAILY_LOSS_PERCENT", "2")),
            max_setups=int(env("MAX_SETUPS", "8")),
            max_lot=float(env("MAX_LOT", "2.0")),
            slippage_points=float(env("SLIPPAGE_POINTS", "1.0")),
            max_stop_points=float(env("MAX_STOP_POINTS", "60")),
            allow_real=env("ALLOW_REAL_ACCOUNT", "false").lower() == "true",
            run_source=env("RUN_SOURCE", "schedule"),
            style=env("STYLE", "standard").lower(),
            target_points=float(env("TARGET_POINTS", "5")),
            emergency_stop_points=float(env("EMERGENCY_STOP_POINTS", "50")),
            max_trades=int(env("MAX_TRADES", "60")),
            spread_mult=float(env("SPREAD_MULT", "1.5")),
            loss_pause_minutes=int(env("LOSS_PAUSE_MINUTES", "15")),
            signal_tf=int(env("AGGR_TIMEFRAME", "5")),
            trend_band=float(env("TREND_BAND", "1.0")),
            trend_lookback=int(env("TREND_LOOKBACK", "14")),
            stack_count=int(env("STACK_COUNT", "8")),
            lot_per_1000=float(env("LOT_PER_1000", "0.1")),
            basket_tp_percent=float(env("BASKET_TP_PERCENT", "1.5")),
            basket_sl_percent=float(env("BASKET_SL_PERCENT", "10")),
            stack_tf=int(env("STACK_TIMEFRAME", "1")),
            stack_counts=env("STACK_COUNTS", "5,8,10"),
            stack_exit=env("STACK_EXIT", "position").lower(),
            position_tp_points=float(env("POSITION_TP_POINTS", "10")),
            stack_breakeven=env("STACK_BREAKEVEN", "true").lower() == "true",
            stack_tp_step=float(env("STACK_TP_STEP", "3")),
            fast_entry=env("FAST_ENTRY", "true").lower() == "true",
            burst_k=float(env("BURST_K", "0.6")),
            adaptive_targets=env("ADAPTIVE_TARGETS", "true").lower() == "true",
            streaming=env("PRICE_STREAMING", "true").lower() == "true",
            regime_filter=env("REGIME_FILTER", "true").lower() == "true",
            mtf_confirm=env("MTF_CONFIRM", "true").lower() == "true",
            self_learning=env("SELF_LEARNING", "true").lower() == "true",
            trailing_lock=env("TRAILING_LOCK", "true").lower() == "true",
            stack_trend_filter=env("STACK_TREND_FILTER", "true").lower() == "true",
        )
        if not env("RISK_PERCENT"):
            s.risk_percent = default_risk(s.style)
        apply_style_defaults(s)
        return s


# ================================================================ pure logic
@dataclass
class Candle:
    t: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    ticks: int = 0
    vol: int = 0

    def update(self, price):
        if self.ticks == 0:
            self.open = self.high = self.low = price
        if self.ticks == 0 or price != self.close:
            self.vol += 1
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.ticks += 1


def ema_series(values, period):
    out, k, e = [], 2 / (period + 1), None
    for v in values:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out


def orb_direction(c: Candle, doji_ratio=0.1):
    rng = c.high - c.low
    if c.ticks < 10 or rng <= 0 or abs(c.close - c.open) < doji_ratio * rng:
        return None
    return "buy" if c.close > c.open else "sell"


def trend_state(candles):
    """Return ('up'|'down'|None, ema9, ema21) from closed 1-minute candles."""
    if len(candles) < 26:
        return None, None, None
    closes = [c.close for c in candles]
    e9, e21 = ema_series(closes, 9), ema_series(closes, 21)
    rising = e21[-1] > e21[-6]
    falling = e21[-1] < e21[-6]
    if e9[-1] > e21[-1] and rising:
        return "up", e9[-1], e21[-1]
    if e9[-1] < e21[-1] and falling:
        return "down", e9[-1], e21[-1]
    return None, e9[-1], e21[-1]


def scalp_signal(candles):
    """Pullback-to-EMA9 entry in the trend direction. Returns (side, stop) or (None, None)."""
    trend, _, _ = trend_state(candles)
    if trend is None:
        return None, None
    e9 = ema_series([c.close for c in candles], 9)
    prev, cur = candles[-2], candles[-1]
    last3 = candles[-3:]
    if trend == "up" and prev.low <= e9[-2] and cur.close > e9[-1] and cur.close > cur.open:
        return "buy", min(c.low for c in last3)
    if trend == "down" and prev.high >= e9[-2] and cur.close < e9[-1] and cur.close < cur.open:
        return "sell", max(c.high for c in last3)
    return None, None


def vwap_series(candles, session_start="09:30"):
    """Session VWAP from 1-minute candles, weighted by tick volume (CFDs have no real volume).
    Returns a list aligned with candles (None before the session starts)."""
    out, pv, vv = [], 0.0, 0.0
    for c in candles:
        if c.t < session_start:
            out.append(None)
            continue
        w = max(c.vol, 1)
        pv += (c.high + c.low + c.close) / 3 * w
        vv += w
        out.append(pv / vv)
    return out


def aggressive_signal(candles):
    """EMA9 + VWAP trend scalp on the last closed 1-minute candle.
    BUY : close > EMA9 > VWAP, EMA9 rising, green candle, not over-extended.
    SELL: mirror image. Returns 'buy', 'sell' or None."""
    if len(candles) < 12:
        return None
    vw = vwap_series(candles)
    if vw[-1] is None or sum(v is not None for v in vw) < 3:
        return None
    e9 = ema_series([c.close for c in candles], 9)
    cur = candles[-1]
    avg_range = sum(c.high - c.low for c in candles[-10:]) / 10 or 1e-9
    stretch = abs(cur.close - e9[-1])
    if stretch > 1.5 * avg_range:
        return None                      # do not chase a candle that already ran away
    if cur.close > e9[-1] > vw[-1] and e9[-1] > e9[-2] and cur.close > cur.open:
        return "buy"
    if cur.close < e9[-1] < vw[-1] and e9[-1] < e9[-2] and cur.close < cur.open:
        return "sell"
    return None


def aggregate(candles, tf):
    """Merge 1-minute candles into tf-minute candles (e.g. 5-minute)."""
    if tf <= 1:
        return list(candles)
    out, cur = [], None
    for c in candles:
        hh, mm = map(int, c.t.split(":"))
        key = f"{hh:02d}:{mm - mm % tf:02d}"
        if cur is None or cur.t != key:
            if cur:
                out.append(cur)
            cur = Candle(t=key, open=c.open, high=c.high, low=c.low, close=c.close,
                         ticks=c.ticks, vol=c.vol)
        else:
            cur.high, cur.low, cur.close = max(cur.high, c.high), min(cur.low, c.low), c.close
            cur.ticks += c.ticks
            cur.vol += c.vol
    if cur:
        out.append(cur)
    return out


ULTRA = {  # STACK_PRESET=ultra : maximum aggression, limits removed (demo lock still applies)
    "lot_per_1000": 0.5, "basket_sl_percent": 30.0, "daily_loss_percent": 100.0,
    "max_trades": 500, "max_lot": 100.0, "stack_trend_filter": False, "loss_pause_minutes": 0,
}


def apply_style_defaults(s):
    """Stack mode accepts big swings: give it a wider daily limit unless the user set one."""
    if s.style == "stack" and (env("STACK_PRESET", "") or "").lower() == "ultra":
        names = {"lot_per_1000": "LOT_PER_1000", "basket_sl_percent": "BASKET_SL_PERCENT",
                 "daily_loss_percent": "DAILY_LOSS_PERCENT", "max_trades": "MAX_TRADES",
                 "max_lot": "MAX_LOT", "stack_trend_filter": "STACK_TREND_FILTER",
                 "loss_pause_minutes": "LOSS_PAUSE_MINUTES"}
        for k, v in ULTRA.items():
            if not env(names[k]):           # a variable you set yourself still wins
                setattr(s, k, v)
        return
    if s.style == "stack" and not env("DAILY_LOSS_PERCENT"):
        s.daily_loss_percent = 30.0
    if s.style == "stack" and not env("MAX_LOT"):
        s.max_lot = 50.0


def efficiency_ratio(candles, n=20):
    """1.0 = price moved in a straight line (clean trend), ~0 = went nowhere (chop)."""
    c = [x.close for x in candles[-(n + 1):]]
    if len(c) < 6:
        return None
    path = sum(abs(c[i] - c[i - 1]) for i in range(1, len(c)))
    return abs(c[-1] - c[0]) / path if path > 0 else 0.0


def market_regime(candles):
    er = efficiency_ratio(candles)
    if er is None:
        return "unknown", None
    if er >= 0.35:
        return "trend", er
    if er < 0.18:
        return "chop", er
    return "mixed", er


def tf_trend(candles, tf):
    """'up' / 'down' / None for a higher timeframe built from 1-minute candles."""
    agg = aggregate([c for c in candles if c.t >= "08:30"], tf)
    if len(agg) < 3:
        return None
    e = ema_series([c.close for c in agg], 9)
    if agg[-1].close > e[-1] and e[-1] >= e[-2]:
        return "up"
    if agg[-1].close < e[-1] and e[-1] <= e[-2]:
        return "down"
    return None


def learn_key(t, regime):
    return f"{t.hour:02d}:{0 if t.minute < 30 else 30:02d}|{regime}"


def learned_block(stats, key, min_n=8):
    """True if this time-slot/regime has enough history and loses money overall."""
    st = (stats or {}).get(key)
    return bool(st and st.get("n", 0) >= min_n and st.get("pnl", 0) < 0)


def avg_range(candles, n=14):
    c = candles[-n:]
    return sum(x.high - x.low for x in c) / len(c) if c else 0.0


def adaptive_tp(s, candles):
    """First target and step between targets, scaled to how fast the market moves right now."""
    if not s.adaptive_targets or len(candles) < 5:
        return s.position_tp_points, s.stack_tp_step
    atr = avg_range(candles)
    first = min(max(0.8 * atr, 5.0), 40.0)
    step = min(max(0.25 * atr, 1.0), 10.0)
    return round(first, 1), round(step, 1)


def burst_direction(candles, recent, now_t, bid, k):
    """Fast move in the trend direction during the last 10 seconds -> 'buy'/'sell'/None."""
    if len(candles) < 12:
        return None
    vw = vwap_series(candles)[-1]
    if vw is None:
        return None
    e9 = ema_series([c.close for c in candles], 9)
    atr = avg_range(candles)
    past = None
    for tt, px in recent:
        if (now_t - tt).total_seconds() >= 10:
            past = px
        else:
            break
    if past is None or atr <= 0:
        return None
    move = bid - past
    if e9[-1] > vw and e9[-1] > e9[-2] and bid > e9[-1] and move >= k * atr:
        return "buy"
    if e9[-1] < vw and e9[-1] < e9[-2] and bid < e9[-1] and -move >= k * atr:
        return "sell"
    return None


def stack_count_for(s, win_streak):
    """5 -> 8 -> 10 positions as winning baskets follow each other; back to the first after a loss."""
    try:
        counts = [int(x) for x in s.stack_counts.split(",") if x.strip()]
    except ValueError:
        counts = []
    if not counts:
        return max(1, s.stack_count)
    return counts[min(win_streak, len(counts) - 1)]


def stack_plan(balance, spec, s, count=None):
    """Per-position volume (compounding with balance), count, and basket money targets."""
    step = float(spec.get("volumeStep") or 0.01)
    vmin = float(spec.get("minVolume") or 0.01)
    per = round_down(balance / 1000.0 * s.lot_per_1000, step)
    per = max(per, vmin)
    n = max(1, count or s.stack_count)
    while n > 1 and per * n > s.max_lot:
        n -= 1
    contract = float(spec.get("contractSize") or 1.0)
    total = per * n
    tp_money = balance * s.basket_tp_percent / 100
    sl_money = balance * s.basket_sl_percent / 100
    sl_points = sl_money / (total * contract)          # server-side stop distance for each position
    return per, n, tp_money, sl_money, sl_points


def basket_pnl(basket, bid, ask, contract):
    sign = 1 if basket["side"] == "buy" else -1
    exit_px = bid if basket["side"] == "buy" else ask
    return sum(sign * (exit_px - e) * v * contract for v, e in basket["legs"])


def default_risk(style):
    return {"aggressive": 0.2, "trend": 1.0}.get(style, 0.5)


def noise_profile(sessions, lookback=14):
    """sessions: list (oldest->newest) of lists of (open, close) per 5-min bar from 09:30.
    Returns sigma per bar index = average |close_k / open_day - 1| over the last `lookback` days."""
    hist = [s for s in sessions if len(s) >= 70][-lookback:]
    if len(hist) < 5:
        return None
    n = min(len(s) for s in hist)
    return [sum(abs(s[k][1] / s[0][0] - 1) for s in hist) / len(hist) for k in range(n)]


def noise_bounds(open_px, prev_close, sigma_k, band):
    up = max(open_px, prev_close) * (1 + band * sigma_k)
    lo = min(open_px, prev_close) * (1 - band * sigma_k)
    return up, lo


def trend_decision(pos, close, up, lo, vwap, check_time):
    """pos: 'buy'/'sell'/None. Returns 'exit', 'buy', 'sell' or None."""
    if pos == "buy" and close < max(up, vwap):
        return "exit"
    if pos == "sell" and close > min(lo, vwap):
        return "exit"
    if pos is None and check_time:
        if close > max(up, vwap):          # breakout AND on the right side of VWAP
            return "buy"
        if close < min(lo, vwap):
            return "sell"
    return None


def fetch_nq_noise(lookback=14):
    """Download recent Nasdaq-100 futures 5-minute bars (free, Yahoo) to measure normal noise.
    Returns (sigma list, gap ratio today or None). Percent moves of NQ and the US100 CFD are
    practically identical, so the profile transfers to USTEC."""
    import yfinance as yf
    df = yf.download("NQ=F", interval="5m", period="30d", progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df.index = df.index.tz_convert("America/New_York")
    today = now_ny().date()
    days, closes = {}, {}
    for ts, r in df.iterrows():
        if ts.weekday() >= 5:
            continue
        hm = ts.strftime("%H:%M")
        if "09:30" <= hm <= "15:55":
            days.setdefault(ts.date(), []).append((float(r["Open"]), float(r["Close"])))
        if hm == "15:55":
            closes[ts.date()] = float(r["Close"])
    past = [days[d] for d in sorted(days) if d < today]
    sigma = noise_profile(past, lookback)
    gap = None
    prev = [d for d in sorted(closes) if d < today]
    if today in days and prev:
        gap = days[today][0][0] / closes[prev[-1]]
    return sigma, gap


def spread_ok(spread, history, mult):
    if len(history) < 30:
        return True
    med = sorted(history)[len(history) // 2]
    return spread <= max(med * mult, med + 0.5)


def aggressive_levels(side, bid, ask, target, stop):
    """Take-profit sits on the broker's server at +target points of REAL profit:
    a buy fills at ask and closes at bid, so TP = ask + target already covers the spread."""
    if side == "buy":
        return ask, ask + target, ask - stop
    return bid, bid - target, bid + stop


def round_down(x, step):
    step = step if step > 0 else 0.01
    v = math.floor(x / step + 1e-9) * step
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return round(v, decimals)


def stack_volumes(risk_money, risk_points, spec, stack_size, max_lot):
    """Split one risk budget across a stack. Returns list of volumes ([] = skip)."""
    contract = float(spec.get("contractSize") or 1.0)
    vmin = float(spec.get("minVolume") or 0.01)
    step = float(spec.get("volumeStep") or 0.01)
    total = min(risk_money / (risk_points * contract), max_lot)
    n = max(1, stack_size)
    per = round_down(total / n, step)
    if per < vmin:
        n = int(total // vmin)
        per = vmin
        if n == 0:
            if vmin * risk_points * contract <= 1.5 * risk_money:
                n = 1
            else:
                return []
    return [per] * n


def tp_ladder(entry, stop, side, n, r_from, r_to):
    risk = abs(entry - stop)
    sign = 1 if side == "buy" else -1
    if n == 1:
        rs = [r_to]
    else:
        rs = [r_from + (r_to - r_from) * i / (n - 1) for i in range(n)]
    return [entry + sign * r * risk for r in rs]


def check_stop(side, entry, stop, spread, s: Settings, max_stop=None):
    risk = (entry - stop) if side == "buy" else (stop - entry)
    cost = spread + 2 * s.slippage_points
    if risk <= 0:
        return None, "price already beyond stop"
    if risk < 2 * cost:
        return None, f"stop {risk:.1f} pts too tight vs costs {cost:.1f}"
    if max_stop and risk > max_stop:
        return None, f"stop {risk:.1f} pts wider than {max_stop:.0f} max"
    return risk, None


def is_demo_server(server):
    s = (server or "").lower()
    return "demo" in s or "trial" in s


def at(day, hh, mm):
    return datetime.combine(day, time(hh, mm), NY)


# ================================================================ GitHub I/O (control + status)
class GitHub:
    def __init__(self):
        self.token = env("GITHUB_TOKEN")
        self.repo = env("GITHUB_REPOSITORY")
        self.enabled = bool(self.token and self.repo)
        self.status_sha = None
        self.branch_ready = False

    def _req(self, method, path, body=None):
        url = f"https://api.github.com/repos/{self.repo}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "nas100-bot"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read() or b"{}")

    def read_control(self):
        if not self.enabled:
            return {"enabled": True}
        try:
            d = self._req("GET", "/contents/control.json")
            return json.loads(base64.b64decode(d["content"]))
        except Exception as e:
            print(f"(control read failed: {e}) - keeping last state")
            return None

    def _ensure_branch(self):
        try:
            self._req("GET", "/git/ref/heads/status")
        except urllib.error.HTTPError:
            repo = self._req("GET", "")
            main = self._req("GET", f"/git/ref/heads/{repo['default_branch']}")
            self._req("POST", "/git/refs", {"ref": "refs/heads/status",
                                             "sha": main["object"]["sha"]})
        self.branch_ready = True

    def read_json(self, path):
        if not self.enabled:
            return None
        try:
            d = self._req("GET", f"/contents/{path}?ref=status")
            return json.loads(base64.b64decode(d["content"]))
        except Exception:
            return None

    def write_json(self, path, obj):
        if not self.enabled:
            return
        try:
            if not self.branch_ready:
                self._ensure_branch()
            sha = None
            try:
                sha = self._req("GET", f"/contents/{path}?ref=status")["sha"]
            except urllib.error.HTTPError:
                pass
            body = {"message": f"update {path}", "branch": "status",
                    "content": base64.b64encode(json.dumps(obj).encode()).decode()}
            if sha:
                body["sha"] = sha
            self._req("PUT", f"/contents/{path}", body)
        except Exception as e:
            print(f"({path} write failed: {e})")

    def write_status(self, status: dict):
        if not self.enabled:
            return
        try:
            if not self.branch_ready:
                self._ensure_branch()
            if self.status_sha is None:
                try:
                    self.status_sha = self._req("GET", "/contents/status.json?ref=status")["sha"]
                except urllib.error.HTTPError:
                    self.status_sha = None
            body = {"message": "status", "branch": "status",
                    "content": base64.b64encode(json.dumps(status).encode()).decode()}
            if self.status_sha:
                body["sha"] = self.status_sha
            r = self._req("PUT", "/contents/status.json", body)
            self.status_sha = r["content"]["sha"]
        except Exception as e:
            print(f"(status write failed: {e})")
            self.status_sha = None


# ================================================================ broker (MetaApi)
class Broker:
    def __init__(self, s: Settings):
        self.s, self.account, self.conn = s, None, None

    async def __aenter__(self):
        from metaapi_cloud_sdk import MetaApi
        api = MetaApi(token=self.s.token)
        self.account = await api.metatrader_account_api.get_account(self.s.account_id)
        if self.account.state not in ("DEPLOYING", "DEPLOYED"):
            await self.account.deploy()
        await self.account.wait_connected()
        self.conn = self.account.get_rpc_connection()
        await self.conn.connect()
        await self.conn.wait_synchronized()
        self.stream, self.fast = None, False
        if self.s.streaming:
            try:   # live price stream: prices arrive the instant they change
                self.stream = self.account.get_streaming_connection()
                await self.stream.connect()
                await self.stream.wait_synchronized()
                await self.stream.subscribe_to_market_data(self.s.symbol)
                self.fast = True
            except Exception as e:
                print(f"(price stream unavailable, polling instead: {e})")
                self.stream = None
        return self

    async def __aexit__(self, *exc):
        try:
            if getattr(self, "stream", None):
                try:
                    await self.stream.close()
                except Exception:
                    pass
            if self.conn:
                await self.conn.close()
        finally:
            if self.account:
                await self.account.undeploy()

    async def info(self):
        return await self.conn.get_account_information()

    async def spec(self):
        return await self.conn.get_symbol_specification(symbol=self.s.symbol)

    async def price(self):
        if getattr(self, "stream", None):
            try:
                p = self.stream.terminal_state.price(self.s.symbol)
                if p and p.get("bid") and p.get("ask"):
                    return p
            except Exception:
                pass
        return await self.conn.get_symbol_price(symbol=self.s.symbol)

    async def bot_positions(self):
        out = []
        for p in await self.conn.get_positions():
            tag = f"{p.get('comment') or ''} {p.get('clientId') or ''}"
            if p.get("symbol") == self.s.symbol and (TAG in tag or "NB" in tag):
                out.append(p)
        return out

    async def market(self, side, volume, sl, tp, client_id):
        fn = self.conn.create_market_buy_order if side == "buy" else self.conn.create_market_sell_order
        return await fn(symbol=self.s.symbol, volume=volume, stop_loss=sl, take_profit=tp,
                        options={"comment": TAG, "clientId": client_id})

    async def modify(self, position_id, sl, tp):
        return await self.conn.modify_position(position_id=position_id, stop_loss=sl, take_profit=tp)

    async def close(self, position_id):
        return await self.conn.close_position(position_id=position_id)


# ================================================================ trading engine
@dataclass
class Setup:
    sid: str
    kind: str
    side: str
    entry: float
    stop: float
    count: int
    be_done: bool = False


@dataclass
class Engine:
    s: Settings
    b: Broker
    gh: GitHub
    spec: dict
    start_balance: float
    digits: int = 2
    candles: list = field(default_factory=list)
    cur: Candle = None
    orb: Candle = field(default_factory=Candle)
    orb_done: bool = False
    setups: list = field(default_factory=list)
    last_setup_closed: datetime = None
    halted: str = None
    log: deque = field(default_factory=lambda: deque(maxlen=25))
    bid: float = 0.0
    ask: float = 0.0
    info: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)
    message: str = "Starting"
    # aggressive-mode state
    spreads: deque = field(default_factory=lambda: deque(maxlen=300))
    trades_today: int = 0
    wins: int = 0
    losses: int = 0
    loss_streak: int = 0
    paused_until: datetime = None
    open_trade: dict = None
    # trend-mode state
    sigma: list = None
    gap: float = None
    trend_pos: str = None
    trend_levels: dict = field(default_factory=dict)
    # stack-mode state
    basket: dict = None
    win_streak: int = 0
    recent: deque = field(default_factory=lambda: deque(maxlen=400))
    last_burst: datetime = None
    regime: str = "unknown"
    er: float = None
    mtf: dict = field(default_factory=dict)
    stats: dict = field(default_factory=dict)
    stats_dirty: bool = False
    last_trail: datetime = None

    def say(self, text):
        line = f"{now_ny():%H:%M:%S} {text}"
        print(line, flush=True)
        self.log.append(line)
        self.message = text

    # ---- candles
    def on_tick(self, t: datetime, bid: float, ask: float):
        self.bid, self.ask = bid, ask
        self.spreads.append(ask - bid)
        self.recent.append((t, bid))
        minute = t.strftime("%H:%M")
        closed = None
        if self.cur is None or self.cur.t != minute:
            if self.cur is not None and self.cur.ticks:
                self.candles.append(self.cur)
                self.candles = self.candles[-240:]
                closed = self.cur
            self.cur = Candle(t=minute)
        self.cur.update(bid)
        open_t = at(t.date(), 9, 30)
        if open_t <= t < open_t + timedelta(minutes=5):
            self.orb.update(bid)
        return closed

    # ---- order placement
    async def open_stack(self, kind, side, stop, r_from, r_to, max_stop=None):
        entry = self.ask if side == "buy" else self.bid
        risk, why = check_stop(side, entry, stop, self.ask - self.bid, self.s, max_stop)
        if risk is None:
            self.say(f"{kind}: skipped ({why})")
            return
        risk_money = self.start_balance * self.s.risk_percent / 100
        vols = stack_volumes(risk_money, risk, self.spec, self.s.stack_size, self.s.max_lot)
        if not vols:
            self.say(f"{kind}: skipped (account too small for minimum lot)")
            return
        tps = tp_ladder(entry, stop, side, len(vols), r_from, r_to)
        sid = f"NB{now_ny():%d%H%M}"
        opened = 0
        for i, (v, tp) in enumerate(zip(vols, tps)):
            try:
                await self.b.market(side, v, round(stop, self.digits), round(tp, self.digits),
                                    f"{sid}_{i}")
                opened += 1
            except Exception as e:
                self.say(f"order {i + 1} failed: {e}")
        if opened:
            self.setups.append(Setup(sid, kind, side, entry, stop, opened))
            self.last_setup_closed = None
            try:
                self.positions = await self.b.bot_positions()
            except Exception:
                pass
            self.say(f"{kind}: {side.upper()} {opened} x {vols[0]} lot @ {entry:.2f}, "
                     f"SL {stop:.2f}, TP {tps[0]:.2f}..{tps[-1]:.2f}")

    # ---- risk management on every refresh
    async def manage(self):
        equity = float(self.info.get("equity", self.start_balance))
        dd = (equity - self.start_balance) / self.start_balance * 100 if self.start_balance else 0
        if not self.halted and dd <= -self.s.daily_loss_percent:
            self.halted = f"daily loss limit hit ({dd:.2f}%)"
            self.say(f"STOP: {self.halted} - closing everything")
            await self.close_all()
            return
        if self.basket and self.open_trade and not self.positions and \
                (now_ny() - self.open_trade["at"]).total_seconds() >= 10:
            self.basket = None                       # closed by the server-side stops
        if self.open_trade and not self.positions and \
                (now_ny() - self.open_trade["at"]).total_seconds() >= 10:
            pl = float(self.info.get("balance", 0)) - self.open_trade["balance"]
            self.record_result(pl)
            if pl >= 0:
                self.wins += 1
                self.win_streak += 1
                self.loss_streak = 0
                self.say(f"Trade closed in profit: +{pl:.2f}")
            else:
                self.losses += 1
                self.win_streak = 0
                self.loss_streak += 1
                self.say(f"Trade closed at a loss: {pl:.2f}")
                if self.loss_streak >= 2:
                    self.paused_until = now_ny() + timedelta(minutes=self.s.loss_pause_minutes)
                    self.say(f"2 stops in a row - pausing {self.s.loss_pause_minutes} min")
            self.open_trade = None
        open_ids = {}
        for p in self.positions:
            key = str(p.get("clientId") or p.get("comment") or "")
            for st in self.setups:
                if st.sid in key:
                    open_ids.setdefault(st.sid, []).append(p)
        for st in self.setups:
            still = open_ids.get(st.sid, [])
            if not st.be_done and still and len(still) <= st.count / 2:
                for p in still:
                    try:
                        await self.b.modify(p["id"], round(st.entry, self.digits), p.get("takeProfit"))
                    except Exception as e:
                        self.say(f"breakeven move failed: {e}")
                st.be_done = True
                self.say(f"{st.kind}: half the stack took profit - rest moved to breakeven")
        if self.setups and not self.positions and self.last_setup_closed is None:
            self.last_setup_closed = now_ny()

    async def close_all(self):
        pos = await self.b.bot_positions()
        res = await asyncio.gather(*[self.b.close(p["id"]) for p in pos], return_exceptions=True)
        for p, r in zip(pos, res):
            if isinstance(r, Exception):
                self.say(f"close failed {p.get('id')}: {r}")

    # ---- aggressive scalping: one position at a time, TP/SL live on the broker server
    async def aggressive_step(self, t: datetime):
        day = t.date()
        if not (at(day, 9, 35) <= t <= at(day, 14, 45)):
            return
        if self.positions or self.open_trade:
            return
        if self.trades_today >= self.s.max_trades:
            return
        if self.paused_until and t < self.paused_until:
            return
        tf = max(1, self.s.signal_tf)
        last_minute = int(self.candles[-1].t[3:]) if self.candles else 0
        if tf > 1 and last_minute % tf != tf - 1:
            return                                  # wait for the 5-minute candle to close
        side = aggressive_signal(aggregate(self.candles, tf))
        if not side:
            return
        spread = self.ask - self.bid
        if not spread_ok(spread, list(self.spreads), self.s.spread_mult):
            self.say(f"{side.upper()} signal skipped - spread {spread:.1f} pts is too wide right now")
            return
        entry, tp, sl = aggressive_levels(side, self.bid, self.ask,
                                          self.s.target_points, self.s.emergency_stop_points)
        risk_money = self.start_balance * self.s.risk_percent / 100
        vols = stack_volumes(risk_money, self.s.emergency_stop_points, self.spec, 1, self.s.max_lot)
        if not vols:
            self.say("Skipped: account too small for the minimum lot at this risk")
            return
        balance = float(self.info.get("balance", self.start_balance))
        try:
            await self.b.market(side, vols[0], round(sl, self.digits), round(tp, self.digits),
                                f"NB{t:%d%H%M}A")
        except Exception as e:
            self.say(f"order failed: {e}")
            return
        self.trades_today += 1
        self.open_trade = {"balance": balance, "at": now_ny(), "side": side}
        self.say(f"#{self.trades_today} {side.upper()} {vols[0]} lot @ {entry:.2f} | "
                 f"TP {tp:.2f} (+{self.s.target_points:g} pts) | SL {sl:.2f}")
        try:
            self.positions = await self.b.bot_positions()
        except Exception:
            pass

    # ---- trend following: noise-area breakout, ride the move, trailing exit
    def trend_state_now(self):
        sess = [c for c in self.candles if c.t >= "09:30"]
        if not sess or not self.sigma:
            return None
        bars = aggregate(sess, 5)
        k = len(bars) - 1
        if k >= len(self.sigma):
            return None
        open_px = bars[0].open
        prev_close = open_px / self.gap if self.gap else open_px
        up, lo = noise_bounds(open_px, prev_close, self.sigma[k], self.s.trend_band)
        vw = vwap_series(bars)[-1]
        return {"k": k, "close": bars[-1].close, "up": up, "lo": lo, "vwap": vw}

    async def trend_step(self, t: datetime):
        if not self.candles or int(self.candles[-1].t[3:]) % 5 != 4:
            return                                    # act on 5-minute closes only
        st = self.trend_state_now()
        if st is None:
            return
        self.trend_levels = st
        day = t.date()
        if self.trend_pos and not self.positions and not self.open_trade:
            self.trend_pos = None                     # closed by the server-side disaster stop
        check = (st["k"] + 1) % 6 == 0 and t <= at(day, 14, 31) and not self.halted
        d = trend_decision(self.trend_pos, st["close"], st["up"], st["lo"], st["vwap"], check)
        if d == "exit":
            await self.close_all()
            self.say(f"Trend over (price back inside noise/VWAP) - position closed")
            self.trend_pos = None
            try:
                self.positions = await self.b.bot_positions()
            except Exception:
                pass
            return
        if d in ("buy", "sell"):
            side = d
            entry = self.ask if side == "buy" else self.bid
            trail = max(st["up"], st["vwap"]) if side == "buy" else min(st["lo"], st["vwap"])
            noise_pts = st["up"] - st["lo"]
            # distance to the disaster stop: never tighter than half the noise area or 20 pts
            dist = 1.5 * max(abs(entry - trail) + (self.ask - self.bid), noise_pts / 2, 20.0)
            balance = float(self.info.get("balance", self.start_balance))
            risk_money = balance * self.s.risk_percent / 100       # compounds with the balance
            vols = stack_volumes(risk_money, dist, self.spec, 1, self.s.max_lot)
            if not vols:
                self.say("Trend signal skipped: account too small for minimum lot")
                return
            disaster = entry - dist if side == "buy" else entry + dist
            try:
                await self.b.market(side, vols[0], round(disaster, self.digits), None,
                                    f"NB{t:%d%H%M}T")
            except Exception as e:
                self.say(f"order failed: {e}")
                return
            self.trades_today += 1
            self.trend_pos = side
            self.open_trade = {"balance": balance, "at": now_ny(), "side": side}
            self.say(f"Breakout {'UP' if side == 'buy' else 'DOWN'}: {side.upper()} {vols[0]} lot "
                     f"@ {entry:.2f}, riding the trend (exit {'below' if side == 'buy' else 'above'} "
                     f"{trail:.2f}, max loss {self.s.risk_percent:g}%)")
            try:
                self.positions = await self.b.bot_positions()
            except Exception:
                pass

    # ---- stack & compound: many positions, one basket target, checked every second
    async def stack_step(self, t: datetime, forced_side=None):
        day = t.date()
        if not (at(day, 9, 35) <= t <= at(day, 14, 45)) or self.halted:
            return
        if self.positions or self.open_trade or self.basket:
            return
        if self.trades_today >= self.s.max_trades:
            return
        tf = max(1, self.s.stack_tf)
        if forced_side:
            side = forced_side
        else:
            if tf > 1 and int(self.candles[-1].t[3:]) % tf != tf - 1:
                return
            side = aggressive_signal(aggregate(self.candles, tf))
        if not side:
            return
        if self.s.stack_trend_filter and self.sigma:
            st = self.trend_state_now()
            if st:
                self.trend_levels = st
                ok = (side == "buy" and st["close"] > max(st["up"], st["vwap"])) or \
                     (side == "sell" and st["close"] < min(st["lo"], st["vwap"]))
                if not ok:
                    return                      # market still inside its normal noise: no basket
        allowed, cap, why = self.gate(side, t)
        if not allowed:
            return
        spread = self.ask - self.bid
        if not spread_ok(spread, list(self.spreads), self.s.spread_mult):
            self.say(f"{side.upper()} signal skipped - spread {spread:.1f} pts too wide")
            return
        balance = float(self.info.get("balance", self.start_balance))
        count = stack_count_for(self.s, self.win_streak)
        if cap:
            count = min(count, cap)
        per, n, tp_money, sl_money, sl_pts = stack_plan(balance, self.spec, self.s, count)
        per_pos = self.s.stack_exit == "position"
        first_tp, tp_step = adaptive_tp(self.s, self.candles)
        entry = self.ask if side == "buy" else self.bid
        sl = entry - sl_pts if side == "buy" else entry + sl_pts
        orders = []
        for i in range(n):
            # each position closes by itself on Exness's server as soon as it reaches its target
            tp = None
            if per_pos:
                dist = first_tp + i * tp_step
                tp = entry + dist if side == "buy" else entry - dist
            orders.append(self.b.market(side, per, round(sl, self.digits),
                                        round(tp, self.digits) if tp else None, f"NB{t:%d%H%M%S}S{i}"))
        results = await asyncio.gather(*orders, return_exceptions=True)   # all sent at once
        legs = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                self.say(f"order {i + 1} failed: {r}")
            else:
                legs.append((per, entry))
        if not legs:
            return
        self.trades_today += 1
        self.basket = {"side": side, "legs": legs, "tp": tp_money, "sl": sl_money,
                       "entry": legs[0][1], "be_done": False}
        self.open_trade = {"balance": balance, "at": now_ny(), "side": side,
                           "key": learn_key(t, self.regime)}
        last_tp = first_tp + (len(legs) - 1) * tp_step
        target_txt = (f"closes one by one at +{first_tp:g}..+{last_tp:g} pts" if per_pos
                      else f"target +${tp_money:.2f}")
        self.say(f"{'⚡ Burst' if forced_side else 'Basket'} #{self.trades_today}: {side.upper()} {len(legs)} x {per} lot "
                 f"(total {per * len(legs):.2f}) | {target_txt} | safety stop -${sl_money:.2f}")
        try:
            self.positions = await self.b.bot_positions()
        except Exception:
            pass

    async def trail_basket(self):
        """Move the remaining positions' stop behind the best price (at most every 5 s)."""
        bk = self.basket
        buy = bk["side"] == "buy"
        px = self.bid if buy else self.ask
        bk["best"] = max(bk.get("best", px), px) if buy else min(bk.get("best", px), px)
        now = now_ny()
        if self.last_trail and (now - self.last_trail).total_seconds() < 5:
            return
        gap = max(adaptive_tp(self.s, self.candles)[0], 5.0)       # keep one target of room
        new_sl = bk["best"] - gap if buy else bk["best"] + gap
        cur = bk.get("trail_sl", bk["entry"])
        if (buy and new_sl > cur + 1) or (not buy and new_sl < cur - 1):
            self.last_trail = now
            res = await asyncio.gather(*[self.b.modify(p["id"], round(new_sl, self.digits), p.get("takeProfit"))
                                         for p in self.positions], return_exceptions=True)
            if not any(isinstance(r, Exception) for r in res):
                bk["trail_sl"] = new_sl

    async def burst_check(self, t: datetime):
        if not self.s.fast_entry or self.basket or self.positions or self.open_trade:
            return
        if self.last_burst and (t - self.last_burst).total_seconds() < 60:
            return
        side = burst_direction(self.candles, self.recent, t, self.bid, self.s.burst_k)
        if side:
            self.last_burst = t
            await self.stack_step(t, forced_side=side)

    async def stack_tick(self):
        if not self.basket:
            return
        if self.s.stack_exit == "position":
            # winners close themselves on the server; only the safety stop is watched here
            pnl = sum(float(p.get("profit") or 0) for p in self.positions)
            self.basket["pnl"] = pnl
            self.basket["open"] = len(self.positions)
            hit = "safety stop" if self.positions and pnl <= -self.basket["sl"] else None
            if (self.s.stack_breakeven and not self.basket.get("be_done") and self.positions
                    and len(self.positions) < len(self.basket["legs"])):
                # at least one position already closed in profit -> lock the rest at entry
                for p in self.positions:
                    try:
                        await self.b.modify(p["id"], round(float(p.get("openPrice") or self.basket["entry"]),
                                                           self.digits), p.get("takeProfit"))
                    except Exception as e:
                        self.say(f"breakeven move failed: {e}")
                self.basket["be_done"] = True
                self.basket["best"] = self.bid if self.basket["side"] == "buy" else self.ask
                self.say("First position won - remaining positions locked at breakeven")
            elif self.s.trailing_lock and self.basket.get("be_done") and self.positions:
                await self.trail_basket()
        else:
            contract = float(self.spec.get("contractSize") or 1.0)
            pnl = basket_pnl(self.basket, self.bid, self.ask, contract)
            self.basket["pnl"] = pnl
            hit = "target" if pnl >= self.basket["tp"] else "stop" if pnl <= -self.basket["sl"] else None
        if hit:
            await self.close_all()
            self.say(f"Basket {hit} reached ({pnl:+.2f}) - all positions closed")
            self.basket = None
            try:
                self.positions = await self.b.bot_positions()
            except Exception:
                pass

    def analyse(self):
        """Heavier analysis, once per closed candle; the per-second loop only reads the cache."""
        self.regime, self.er = market_regime(self.candles)
        self.mtf = {"5m": tf_trend(self.candles, 5), "15m": tf_trend(self.candles, 15)}

    def gate(self, side, t):
        """Intelligence checks before a basket. Returns (allowed, basket-count cap, reason)."""
        cap = None
        if self.s.regime_filter:
            if self.regime == "chop":
                return False, None, "market choppy"
            if self.regime == "mixed":
                cap = stack_count_for(self.s, 0)            # smallest basket only
        if self.s.mtf_confirm:
            want = "up" if side == "buy" else "down"
            for tf in ("5m", "15m"):
                if self.mtf.get(tf) and self.mtf[tf] != want:
                    return False, None, f"{tf} trend disagrees"
        if self.s.self_learning and learned_block(self.stats, learn_key(t, self.regime)):
            return False, None, "learned: this time/regime loses"
        return True, cap, ""

    def record_result(self, pl):
        if not self.open_trade:
            return
        key = self.open_trade.get("key")
        if not key:
            return
        st = self.stats.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0})
        st["n"] += 1
        st["wins"] += 1 if pl >= 0 else 0
        st["pnl"] = round(st["pnl"] + pl, 2)
        self.stats_dirty = True

    # ---- decisions on each closed 1-minute candle
    async def on_candle(self, t: datetime):
        self.analyse()
        if self.halted:
            return
        if self.s.style == "aggressive":
            await self.aggressive_step(t)
            return
        if self.s.style == "trend":
            await self.trend_step(t)
            return
        if self.s.style == "stack":
            await self.stack_step(t)
            return
        day = t.date()
        if not self.orb_done and t >= at(day, 9, 35):
            self.orb_done = True
            side = orb_direction(self.orb)
            if side is None:
                self.say("Opening range: no ORB trade (flat candle or bot started after 09:30)")
            else:
                stop = self.orb.low if side == "buy" else self.orb.high
                await self.open_stack("ORB", side, stop, 2.0, 10.0)
            return
        if not (at(day, 9, 40) <= t <= at(day, 14, 45)):
            return
        if self.positions:
            return
        if len(self.setups) >= self.s.max_setups:
            return
        if self.last_setup_closed and t - self.last_setup_closed < timedelta(minutes=3):
            return
        side, stop = scalp_signal(self.candles)
        if side:
            self.last_setup_closed = None
            await self.open_stack("Scalp", side, stop, 1.0, 2.5, self.s.max_stop_points)

    # ---- status for the phone app
    def status(self, state):
        t = now_ny()
        trend, e9, e21 = trend_state(self.candles)
        day = t.date()
        session = at(day, 9, 30) <= t <= at(day, 15, 0)
        closes = [c.close for c in self.candles[-60:]]
        e9s = ema_series([c.close for c in self.candles], 9)[-60:]
        e21s = ema_series([c.close for c in self.candles], 21)[-60:]
        vws = vwap_series(self.candles)[-60:]
        aggressive = self.s.style == "aggressive"
        trend = self.s.style == "trend"
        stack = self.s.style == "stack"
        if stack:
            bk = self.basket
            checks = [
                {"label": "Connected to MT5", "ok": True},
                {"label": "NY session open", "ok": session},
                {"label": (f"Basket {bk['side'].upper()} · {bk.get('open', len(bk['legs']))}/{len(bk['legs'])} "
                           f"open · P/L {bk.get('pnl', 0):+.2f}" if bk else "Scanning for entry"), "ok": bool(bk)},
                {"label": ((f"Each closes at +{self.s.position_tp_points:g} pts · safety -${bk['sl']:.2f}"
                            if self.s.stack_exit == "position" else
                            f"Target +${bk['tp']:.2f} · Stop -${bk['sl']:.2f}") if bk
                           else f"Next basket: {stack_count_for(self.s, self.win_streak)} positions "
                                f"(streak {self.win_streak})"), "ok": True},
                {"label": f"Regime: {self.regime.upper()}" + (f" (ER {self.er:.2f})" if self.er is not None else ""),
                 "ok": self.regime == "trend"},
                {"label": f"5m {self.mtf.get('5m') or '-'} · 15m {self.mtf.get('15m') or '-'}",
                 "ok": bool(self.mtf.get("5m")) and self.mtf.get("5m") == self.mtf.get("15m")},
                {"label": f"Learned from {sum(v.get('n', 0) for v in self.stats.values())} past baskets",
                 "ok": True},
                {"label": "Risk limits OK" if not self.halted else f"Halted: {self.halted}",
                 "ok": not self.halted},
            ]
        elif trend:
            L = self.trend_levels or {}
            inside = L and L["lo"] <= L["close"] <= L["up"]
            checks = [
                {"label": "Connected to MT5", "ok": True},
                {"label": "NY session open", "ok": session},
                {"label": "Noise profile loaded (14 days)" if self.sigma else "Noise profile not loaded",
                 "ok": bool(self.sigma)},
                {"label": (f"Noise area {L['lo']:.0f} – {L['up']:.0f}" if L else "Noise area: waiting for 09:35"),
                 "ok": bool(L)},
                {"label": ("Riding trend " + ("UP" if self.trend_pos == "buy" else "DOWN")) if self.trend_pos
                 else ("Inside noise – no trend yet" if inside or not L else "Breakout – waiting for :00/:30 check"),
                 "ok": bool(self.trend_pos)},
                {"label": "Risk limits OK" if not self.halted else f"Halted: {self.halted}",
                 "ok": not self.halted},
            ]
        elif aggressive and self.candles:
            vw_now = vws[-1] if vws else None
            above = vw_now is not None and self.bid > vw_now
            spread = self.ask - self.bid
            checks = [
                {"label": "Connected to MT5", "ok": True},
                {"label": "NY session open", "ok": session},
                {"label": "VWAP: price " + ("ABOVE (buy side)" if above else "BELOW (sell side)")
                 if vw_now is not None else "VWAP: waiting for 09:30", "ok": vw_now is not None},
                {"label": f"Spread {spread:.1f} pts " + ("OK" if spread_ok(spread, list(self.spreads), self.s.spread_mult) else "too wide"),
                 "ok": spread_ok(spread, list(self.spreads), self.s.spread_mult)},
                {"label": "In trade - TP/SL on Exness server" if self.positions
                 else ("Paused after 2 stops" if self.paused_until and now_ny() < self.paused_until
                       else "Scanning every candle for entry"), "ok": True},
                {"label": "Risk limits OK" if not self.halted else f"Halted: {self.halted}",
                 "ok": not self.halted},
            ]
        equity = float(self.info.get("equity", 0) or 0)
        return {
            "updated": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "message": self.message,
            "symbol": self.s.symbol,
            "bid": self.bid, "ask": self.ask,
            "trend": trend,
            "ema9": e9, "ema21": e21,
            "chart": {"close": closes, "ema9": [] if trend else e9s,
                      "ema21": [] if self.s.style in ("aggressive", "trend", "stack") else e21s,
                      "vwap": vws,
                      "up": (self.trend_levels or {}).get("up"), "lo": (self.trend_levels or {}).get("lo")},
            "style": self.s.style,
            "checks": checks if (trend or stack or (aggressive and self.candles)) else [
                {"label": "Connected to MT5", "ok": True},
                {"label": "NY session open", "ok": session},
                {"label": f"Trend detected: {trend.upper() if trend else 'none'}", "ok": bool(trend)},
                {"label": "Position management active" if self.positions else "Waiting for pullback setup",
                 "ok": bool(self.positions)},
                {"label": "Risk limits OK" if not self.halted else f"Halted: {self.halted}",
                 "ok": not self.halted},
            ],
            "account": {
                "type": "Demo" if is_demo_server(self.info.get("server")) else "Real",
                "server": self.info.get("server"),
                "currency": self.info.get("currency"),
                "balance": self.info.get("balance"),
                "equity": equity,
                "day_pl": round(equity - self.start_balance, 2) if equity else None,
            },
            "positions": [{
                "side": "BUY" if "BUY" in str(p.get("type")) else "SELL",
                "volume": p.get("volume"), "open": p.get("openPrice"),
                "sl": p.get("stopLoss"), "tp": p.get("takeProfit"),
                "profit": p.get("profit")} for p in self.positions],
            "today": ({"setups": self.trades_today, "max_setups": self.s.max_trades,
                       "wins": self.wins, "losses": self.losses}
                      if self.s.style in ("aggressive", "trend", "stack") else
                      {"setups": len(self.setups), "max_setups": self.s.max_setups}),
            "log": list(self.log),
        }


# ================================================================ jobs
async def run_session(s: Settings, broker_factory=Broker, gh=None, clock=now_ny, tick=1.0,
                      noise_fetcher=fetch_nq_noise):
    gh = gh or GitHub()
    t0 = clock()
    day = t0.date()
    start_win, open_t, end_t = at(day, 8, 40), at(day, 9, 30), at(day, 15, 0)

    def idle(msg):
        print(msg)
        gh.write_status({"updated": datetime.now(timezone.utc).isoformat(), "state": "STOPPED",
                         "message": msg, "symbol": s.symbol, "checks": [], "positions": [], "log": [msg]})

    control = gh.read_control() or {"enabled": True}
    if control.get("style") in ("standard", "aggressive", "trend", "stack"):   # chosen in the app
        s.style = control["style"]
        if not env("RISK_PERCENT"):
            s.risk_percent = default_risk(s.style)
        apply_style_defaults(s)
    if not control.get("enabled", False):
        idle("Bot is stopped. Press START in the app.")
        return
    if t0.weekday() >= 5:
        idle("Weekend - market closed. Bot will start Monday 09:15 New York.")
        return
    if s.run_source == "manual":
        if t0 < start_win:
            idle("Armed. Trading starts automatically at 09:15 New York time.")
            return
        if t0 > end_t - timedelta(minutes=10):
            idle("NY session is over for today. Bot will start next trading day.")
            return
    elif not (start_win <= t0 <= open_t - timedelta(minutes=1)):
        print(f"Not this run's window (NY {t0:%H:%M}) - exiting quietly.")
        return

    hard_end = min(end_t, t0 + timedelta(minutes=335))
    async with broker_factory(s) as b:
        info = await b.info()
        if not is_demo_server(info.get("server")) and not s.allow_real:
            idle(f"Blocked: {info.get('server')} is a REAL account. "
                 f"Set ALLOW_REAL_ACCOUNT=true after demo testing.")
            return
        spec = await b.spec()
        eng = Engine(s, b, gh, spec, float(info.get("balance", 0)),
                     digits=int(spec.get("digits") or 2))
        eng.info = info
        eng.positions = await b.bot_positions()
        if s.style == "trend" or (s.style == "stack" and s.stack_trend_filter):
            try:
                eng.sigma, eng.gap = noise_fetcher(s.trend_lookback)
                eng.say(f"Noise profile loaded from {s.trend_lookback} days of Nasdaq data")
            except Exception as e:
                eng.say(f"Could not load noise profile ({e}) - no trend trades today")
        gap_checked = False
        if s.self_learning:
            eng.stats = gh.read_json("learning.json") or {}
        eng.say(f"Connected to {info.get('server')} - balance {info.get('balance')} "
                f"{info.get('currency')} - {s.style} mode")
        gh.write_status(eng.status("RUNNING"))

        last_refresh = last_report = clock()
        refresh_every = 5 if s.style in ("aggressive", "stack") else 30
        stopped_by_user = False
        try:
            while clock() < hard_end:
                t = clock()
                try:
                    p = await b.price()
                    closed = eng.on_tick(t, float(p["bid"]), float(p["ask"]))
                    if s.style == "stack":
                        await eng.stack_tick()
                        await eng.burst_check(t)
                    if (s.style in ("trend", "stack") and not gap_checked and eng.sigma
                            and t >= at(day, 9, 37)):
                        gap_checked = True
                        try:
                            _, g = noise_fetcher(s.trend_lookback)
                            if g:
                                eng.gap = g
                        except Exception:
                            pass
                    if closed is not None:
                        await eng.on_candle(t)
                except Exception as e:
                    print(f"tick error: {e}")
                if (t - last_refresh).total_seconds() >= refresh_every:
                    last_refresh = t
                    try:
                        eng.info = await b.info()
                        eng.positions = await b.bot_positions()
                        await eng.manage()
                    except Exception as e:
                        print(f"refresh error: {e}")
                if (t - last_report).total_seconds() >= 30:
                    last_report = t
                    if eng.stats_dirty:
                        gh.write_json("learning.json", eng.stats)
                        eng.stats_dirty = False
                    ctl = gh.read_control()
                    if ctl is not None and not ctl.get("enabled", False):
                        stopped_by_user = True
                        eng.say("STOP pressed in the app - closing all positions")
                        break
                    gh.write_status(eng.status("RUNNING"))
                await asyncio.sleep(0.2 if getattr(b, "fast", False) else tick)
        finally:
            await eng.close_all()
            eng.positions = []
            try:
                eng.info = await b.info()
            except Exception:
                pass
            if eng.stats_dirty:
                gh.write_json("learning.json", eng.stats)
            eng.say("Stopped by user" if stopped_by_user else "Session finished - all positions closed")
            gh.write_status(eng.status("STOPPED"))


async def run_close(s: Settings, broker_factory=Broker):
    async with broker_factory(s) as b:
        for p in await b.bot_positions():
            try:
                await b.close(p["id"])
                print(f"closed {p.get('id')} P/L {p.get('profit')}")
            except Exception as e:
                print(f"close failed {p.get('id')}: {e}")


async def run_status(s: Settings, broker_factory=Broker):
    async with broker_factory(s) as b:
        info = await b.info()
        print(json.dumps({k: info.get(k) for k in
                          ("server", "balance", "equity", "currency", "leverage")}, indent=2))
        print(json.dumps(await b.spec(), indent=2, default=str))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "status"
    s = Settings.load()
    if not (s.token and s.account_id):
        sys.exit("Missing METAAPI_TOKEN or METAAPI_ACCOUNT_ID secrets.")
    if mode == "run":
        asyncio.run(run_session(s))
    elif mode == "close":
        asyncio.run(run_close(s))
    elif mode == "status":
        asyncio.run(run_status(s))
    else:
        sys.exit("Use: run, close or status")


if __name__ == "__main__":
    main()
