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

    @classmethod
    def load(cls):
        return cls(
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
        )


# ================================================================ pure logic
@dataclass
class Candle:
    t: str = ""
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    ticks: int = 0

    def update(self, price):
        if self.ticks == 0:
            self.open = self.high = self.low = price
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
        return self

    async def __aexit__(self, *exc):
        try:
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

    def say(self, text):
        line = f"{now_ny():%H:%M:%S} {text}"
        print(line, flush=True)
        self.log.append(line)
        self.message = text

    # ---- candles
    def on_tick(self, t: datetime, bid: float, ask: float):
        self.bid, self.ask = bid, ask
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
        for p in await self.b.bot_positions():
            try:
                await self.b.close(p["id"])
            except Exception as e:
                self.say(f"close failed {p.get('id')}: {e}")

    # ---- decisions on each closed 1-minute candle
    async def on_candle(self, t: datetime):
        if self.halted:
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
        equity = float(self.info.get("equity", 0) or 0)
        return {
            "updated": datetime.now(timezone.utc).isoformat(),
            "state": state,
            "message": self.message,
            "symbol": self.s.symbol,
            "bid": self.bid, "ask": self.ask,
            "trend": trend,
            "ema9": e9, "ema21": e21,
            "chart": {"close": closes, "ema9": e9s, "ema21": e21s},
            "checks": [
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
            "today": {"setups": len(self.setups), "max_setups": self.s.max_setups},
            "log": list(self.log),
        }


# ================================================================ jobs
async def run_session(s: Settings, broker_factory=Broker, gh=None, clock=now_ny, tick=1.0):
    gh = gh or GitHub()
    t0 = clock()
    day = t0.date()
    start_win, open_t, end_t = at(day, 8, 40), at(day, 9, 30), at(day, 15, 0)

    def idle(msg):
        print(msg)
        gh.write_status({"updated": datetime.now(timezone.utc).isoformat(), "state": "STOPPED",
                         "message": msg, "symbol": s.symbol, "checks": [], "positions": [], "log": [msg]})

    control = gh.read_control() or {"enabled": True}
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
        eng.say(f"Connected to {info.get('server')} - balance {info.get('balance')} "
                f"{info.get('currency')}")
        gh.write_status(eng.status("RUNNING"))

        last_refresh = clock()
        stopped_by_user = False
        try:
            while clock() < hard_end:
                t = clock()
                try:
                    p = await b.price()
                    closed = eng.on_tick(t, float(p["bid"]), float(p["ask"]))
                    if closed is not None:
                        await eng.on_candle(t)
                except Exception as e:
                    print(f"tick error: {e}")
                if (t - last_refresh).total_seconds() >= 30:
                    last_refresh = t
                    try:
                        eng.info = await b.info()
                        eng.positions = await b.bot_positions()
                        await eng.manage()
                    except Exception as e:
                        print(f"refresh error: {e}")
                    ctl = gh.read_control()
                    if ctl is not None and not ctl.get("enabled", False):
                        stopped_by_user = True
                        eng.say("STOP pressed in the app - closing all positions")
                        break
                    gh.write_status(eng.status("RUNNING"))
                await asyncio.sleep(tick)
        finally:
            await eng.close_all()
            eng.positions = []
            try:
                eng.info = await b.info()
            except Exception:
                pass
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
