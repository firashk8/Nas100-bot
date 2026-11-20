"""
Replay the REAL bot (bot.run_session) on real Nasdaq-100 futures 1-minute prices.
Every 1-minute bar is expanded into 60 one-second prices (open -> high/low -> close),
orders fill at bid/ask with SPREAD and extra SLIP, take-profits / stops trigger on the path.
Balance and the self-learning memory carry over from day to day.
Usage: python backtest/replay.py [stack|ultra|trend|aggressive]
"""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
from backtest import load_real  # noqa: E402

NY = bot.NY
SPREAD = float(os.environ.get("REPLAY_SPREAD", "1.5"))
SLIP = float(os.environ.get("REPLAY_SLIP", "0.5"))
OUT = os.environ.get("OUT_DIR", "results")


def second_path(c):
    pts = [c.open, c.low, c.high, c.close] if c.close >= c.open else [c.open, c.high, c.low, c.close]
    out = []
    for s in range(60):
        seg = min(int(s / 20), 2)
        f = (s - seg * 20) / 20
        out.append(pts[seg] + (pts[seg + 1] - pts[seg]) * f)
    return out


class Clock:
    t = None


class ReplayBroker:
    prices = {}
    balance = 1000.0

    def __init__(self, s):
        self.pos, self.n, self.fast = [], 0, False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def info(self):
        fl = sum(p["profit"] for p in self.pos)
        return {"server": "Replay-Trial", "balance": ReplayBroker.balance,
                "equity": ReplayBroker.balance + fl, "currency": "USD"}

    async def spec(self):
        return {"contractSize": 1.0, "minVolume": 0.01, "maxVolume": 500, "volumeStep": 0.01, "digits": 2}

    def mid(self):
        return ReplayBroker.prices.get(Clock.t.replace(microsecond=0))

    async def price(self):
        m = self.mid()
        if m is None:
            raise RuntimeError("no price")
        bid = m - SPREAD / 2
        ask = m + SPREAD / 2
        for p in list(self.pos):
            buy = p["type"] == "POSITION_TYPE_BUY"
            px = bid if buy else ask
            p["profit"] = round((px - p["openPrice"]) * (1 if buy else -1) * p["volume"], 2)
            sl, tp = p["stopLoss"], p["takeProfit"]
            if sl is not None and ((px <= sl) if buy else (px >= sl)):
                self._book(p, (sl - p["openPrice"]) * (1 if buy else -1) - SLIP)
            elif tp is not None and ((px >= tp) if buy else (px <= tp)):
                self._book(p, (tp - p["openPrice"]) * (1 if buy else -1))
        return {"bid": bid, "ask": ask}

    def _book(self, p, pts):
        ReplayBroker.balance += pts * p["volume"]
        self.pos.remove(p)

    async def bot_positions(self):
        return list(self.pos)

    async def market(self, side, v, sl, tp, cid):
        m = self.mid()
        self.n += 1
        fill = m + SPREAD / 2 + SLIP if side == "buy" else m - SPREAD / 2 - SLIP
        self.pos.append({"id": str(self.n), "type": "POSITION_TYPE_BUY" if side == "buy" else "POSITION_TYPE_SELL",
                         "volume": v, "openPrice": fill, "stopLoss": sl, "takeProfit": tp, "profit": 0.0,
                         "clientId": cid, "comment": bot.TAG, "symbol": "USTEC"})
        return {"stringCode": "TRADE_RETCODE_DONE"}

    async def modify(self, pid, sl, tp):
        for p in self.pos:
            if p["id"] == pid:
                p["stopLoss"], p["takeProfit"] = sl, tp

    async def close(self, pid):
        for p in list(self.pos):
            if p["id"] == pid:
                buy = p["type"] == "POSITION_TYPE_BUY"
                m = self.mid() or p["openPrice"]
                px = m - SPREAD / 2 if buy else m + SPREAD / 2
                self._book(p, (px - p["openPrice"]) * (1 if buy else -1) - SLIP)


class MemGH:
    store = {}

    def __init__(self, style):
        self.style, self.last = style, None

    def read_control(self):
        return {"enabled": True, "style": self.style}

    def write_status(self, st):
        self.last = st

    def read_json(self, path):
        return MemGH.store.get(path)

    def write_json(self, path, obj):
        MemGH.store[path] = json.loads(json.dumps(obj))


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "stack"
    style = "stack" if mode in ("ultra", "stack_raw") else mode
    if mode == "ultra":
        os.environ["STACK_PRESET"] = "ultra"
    days = load_real()
    names = sorted(days)

    async def fast_sleep(x):
        Clock.t += timedelta(seconds=x if x else 1)

    bot.asyncio.sleep = fast_sleep
    bot.now_ny = lambda: Clock.t
    lines = [f"# Replay of the real bot, mode={mode}, spread {SPREAD}, slippage {SLIP}", "",
             "| day | start | end | day % | baskets | wins | losses |", "|---|---|---|---|---|---|---|"]
    for i, d in enumerate(names):
        bars = days[d]
        y, mo, dd = map(int, d.split("-"))
        ReplayBroker.prices = {}
        for c in bars:
            hh, mm = map(int, c.t.split(":"))
            base = datetime(y, mo, dd, hh, mm, tzinfo=NY)
            for sidx, px in enumerate(second_path(c)):
                ReplayBroker.prices[base + timedelta(seconds=sidx)] = px

        prior = [days[n] for n in names[:i]]
        def noise(lb, prior=prior):
            sess = []
            for b in prior:
                agg = bot.aggregate([c for c in b if "09:30" <= c.t <= "15:59"], 5)
                sess.append([(c.open, c.close) for c in agg])
            sigma = bot.noise_profile(sess, lb)
            if sigma is None:
                raise RuntimeError("not enough history")
            return sigma, None

        Clock.t = datetime(y, mo, dd, 9, 12, tzinfo=NY)
        s = bot.Settings(token="x", account_id="y", run_source="schedule")
        if mode == "stack_raw":     # all smart filters and the agent switched off
            s.regime_filter = s.mtf_confirm = s.self_learning = s.agent = s.stack_trend_filter = False
            os.environ["REGIME_FILTER"] = "false"
        start = ReplayBroker.balance
        gh = MemGH(style)
        asyncio.run(bot.run_session(s, ReplayBroker, gh, clock=lambda: Clock.t, noise_fetcher=noise))
        td = (gh.last or {}).get("today", {})
        lines.append(f"| {d} | {start:.2f} | {ReplayBroker.balance:.2f} | "
                     f"{100 * (ReplayBroker.balance / start - 1):+.1f}% | {td.get('setups', 0)} | "
                     f"{td.get('wins', 0)} | {td.get('losses', 0)} |")
        print(lines[-1], flush=True)
        if ReplayBroker.balance < 50:
            lines.append("Account effectively wiped out.")
            break
    lines += ["", f"Final balance: {ReplayBroker.balance:.2f} (from 1000.00)"]
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, f"replay_{mode}.md"), "w").write("\n".join(lines))
    print("\n".join(lines[-2:]))


if __name__ == "__main__":
    main()
