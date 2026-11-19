"""
Real-data backtest of the bot's AGGRESSIVE mode on US100 (Nasdaq-100 futures, NQ=F, 1-minute bars).

It reuses the exact entry logic from bot.py (aggressive_signal), then replays every session
minute by minute and tests many exit rules, with spread and slippage costs.

Conservative assumptions:
  * entries fill at the NEXT bar's open, paying half the spread
  * if a bar touches both stop and target, the STOP is assumed to be hit first
  * stops get extra slippage
  * one position at a time, entries 09:35-14:45 New York, everything closed at 15:00
  * after 2 stop-losses in a row the bot pauses 15 minutes (as in the live bot)

Every rule is compared with a RANDOM-direction baseline taking trades at the same moments,
and checked separately on the first and second half of the sessions (robustness).

Usage:  python backtest/backtest.py            (downloads data with yfinance)
        python backtest/backtest.py --synthetic (offline test with fake data)
"""
import json
import os
import random
import statistics
import sys
from datetime import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bot import Candle, aggressive_signal, ema_series, vwap_series  # noqa: E402

OUT = os.environ.get("OUT_DIR", "results")
SL_SLIP = 1.0                       # extra points lost when a stop is hit
TP_GRID = [3, 5, 8, 12, 20]
SL_GRID = [15, 25, 50]
EXIT_RULES = ["none", "vwap_break", "ema9_break", "time10", "vwap_or_time10", "time20"]
SPREADS = [1.0, 2.0]


# ------------------------------------------------------------------ data
def load_real():
    import pandas as pd
    import yfinance as yf
    frames = []
    end = pd.Timestamp.utcnow().normalize() + pd.Timedelta(days=1)
    for k in range(4):                       # Yahoo serves 1m data for ~30 days, 7 days per call
        e = end - pd.Timedelta(days=7 * k)
        s = e - pd.Timedelta(days=7)
        try:
            df = yf.download("NQ=F", interval="1m", start=s, end=e, progress=False, auto_adjust=False)
            if len(df):
                frames.append(df)
        except Exception as ex:
            print("download failed", s, e, ex)
    if not frames:
        raise SystemExit("No data downloaded")
    df = pd.concat(frames)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df[~df.index.duplicated()].sort_index()
    df.index = df.index.tz_convert("America/New_York")
    days = {}
    for ts, r in df.iterrows():
        t = ts.time()
        if t < time(8, 30) or t >= time(16, 0) or ts.weekday() >= 5:
            continue
        c = Candle(t=ts.strftime("%H:%M"), open=float(r["Open"]), high=float(r["High"]),
                   low=float(r["Low"]), close=float(r["Close"]), ticks=60,
                   vol=int(r["Volume"]) if r["Volume"] == r["Volume"] else 1)
        days.setdefault(ts.date().isoformat(), []).append(c)
    return {d: c for d, c in days.items() if sum(x.t >= "09:30" for x in c) > 300}


def load_synthetic(n_days=20, seed=1):
    rnd = random.Random(seed)
    days, px = {}, 21000.0
    for d in range(n_days):
        bars, drift = [], rnd.gauss(0, 0.6)
        for m in range(8 * 60 + 30, 16 * 60):
            o = px
            path = [o]
            for _ in range(6):
                px += drift * 0.1 + rnd.gauss(0, 2.2)
                path.append(px)
            bars.append(Candle(t=f"{m // 60:02d}:{m % 60:02d}", open=o, high=max(path),
                               low=min(path), close=px, ticks=60, vol=rnd.randint(200, 2000)))
        days[f"2026-01-{d + 1:02d}"] = bars
    return days


# ------------------------------------------------------------------ simulation
def signals_for_day(bars):
    """Signal on bar i (using bars[0..i]) -> entry at bar i+1 open. Computed once per day."""
    out = {}
    for i in range(len(bars) - 1):
        t = bars[i].t
        if "09:34" <= t <= "14:44":
            s = aggressive_signal(bars[: i + 1])
            if s:
                out[i] = s
    return out


def simulate_day(bars, sigs, tp, sl, rule, spread, random_dir=None):
    closes = [b.close for b in bars]
    e9 = ema_series(closes, 9)
    vw = vwap_series(bars)
    half = spread / 2
    trades, i, streak, pause_until = [], 0, 0, -1
    n = len(bars)
    while i < n - 1:
        if i in sigs and i >= pause_until and len(trades) < 60:
            side = sigs[i] if random_dir is None else random_dir.choice(["buy", "sell"])
            j = i + 1
            sign = 1 if side == "buy" else -1
            mid_entry = bars[j].open
            entry = mid_entry + sign * half                   # pay half spread on entry
            tp_mid = entry + sign * tp + sign * half          # exit side pays the other half
            sl_mid = entry - sign * sl + sign * half
            result, k = None, j
            while k < n:
                b = bars[k]
                if b.t >= "15:00":
                    result = sign * (b.open - sign * half - entry)
                    break
                hit_sl = (b.low <= sl_mid) if side == "buy" else (b.high >= sl_mid)
                hit_tp = (b.high >= tp_mid) if side == "buy" else (b.low <= tp_mid)
                if hit_sl:
                    result = -sl - SL_SLIP
                    break
                if hit_tp:
                    result = tp
                    break
                held = k - j + 1
                exit_now = False
                if rule in ("vwap_break", "vwap_or_time10") and vw[k] is not None:
                    exit_now |= (b.close < vw[k]) if side == "buy" else (b.close > vw[k])
                if rule == "ema9_break":
                    exit_now |= (b.close < e9[k]) if side == "buy" else (b.close > e9[k])
                if rule in ("time10", "vwap_or_time10"):
                    exit_now |= held >= 10
                if rule == "time20":
                    exit_now |= held >= 20
                if exit_now:
                    result = sign * (b.close - sign * half - entry)
                    break
                k += 1
            if result is None:
                result = sign * (bars[-1].close - sign * half - entry)
                k = n - 1
            trades.append(result)
            streak = streak + 1 if result < 0 else 0
            if streak >= 2:
                pause_until = k + 15
                streak = 0
            i = k + 1
        else:
            i += 1
    return trades


def stats(pts):
    if not pts:
        return {"n": 0, "exp": 0, "win": 0, "avg_win": 0, "avg_loss": 0, "total": 0, "pf": 0,
                "worst_day": 0}
    wins = [p for p in pts if p > 0]
    losses = [p for p in pts if p <= 0]
    gl = -sum(losses)
    return {"n": len(pts), "exp": round(statistics.mean(pts), 3),
            "win": round(100 * len(wins) / len(pts), 1),
            "avg_win": round(statistics.mean(wins), 2) if wins else 0,
            "avg_loss": round(statistics.mean(losses), 2) if losses else 0,
            "total": round(sum(pts), 1),
            "pf": round(sum(wins) / gl, 2) if gl > 0 else 99}


def run(days):
    names = sorted(days)
    half = len(names) // 2
    sigs = {d: signals_for_day(days[d]) for d in names}
    n_sig = sum(len(s) for s in sigs.values())
    print(f"{len(names)} sessions, {n_sig} raw signals")
    rows = []
    for spread in SPREADS:
        for tp in TP_GRID:
            for sl in SL_GRID:
                for rule in EXIT_RULES:
                    per_day = {d: simulate_day(days[d], sigs[d], tp, sl, rule, spread) for d in names}
                    all_pts = [p for d in names for p in per_day[d]]
                    a = [p for d in names[:half] for p in per_day[d]]
                    b = [p for d in names[half:] for p in per_day[d]]
                    rnd_exp = []
                    for seed in range(10):
                        r = random.Random(seed)
                        rp = [p for d in names
                              for p in simulate_day(days[d], sigs[d], tp, sl, rule, spread, r)]
                        rnd_exp.append(statistics.mean(rp) if rp else 0)
                    day_tot = [sum(per_day[d]) for d in names]
                    st = stats(all_pts)
                    st.update({"spread": spread, "tp": tp, "sl": sl, "rule": rule,
                               "exp_first_half": stats(a)["exp"], "exp_second_half": stats(b)["exp"],
                               "random_exp": round(statistics.mean(rnd_exp), 3),
                               "edge_vs_random": round(st["exp"] - statistics.mean(rnd_exp), 3),
                               "worst_day_pts": round(min(day_tot), 1),
                               "best_day_pts": round(max(day_tot), 1),
                               "green_days": sum(1 for x in day_tot if x > 0)})
                    rows.append(st)
    return names, n_sig, rows


def report(names, n_sig, rows, source):
    os.makedirs(OUT, exist_ok=True)
    json.dump({"sessions": names, "signals": n_sig, "rows": rows, "source": source},
              open(os.path.join(OUT, "results.json"), "w"), indent=1)
    lines = [f"# Aggressive-mode backtest ({source})", "",
             f"Sessions: {len(names)} ({names[0]} to {names[-1]}), raw signals: {n_sig}", "",
             "Points per trade after spread. exp = average points per trade. "
             "edge = exp minus random-direction entries with the same exits.", ""]
    hdr = "| spread | TP | SL | exit rule | trades | win% | avg win | avg loss | exp | 1st half | 2nd half | random | edge | worst day | green days |"
    sep = "|" + "---|" * 15
    for spread in SPREADS:
        sub = sorted([r for r in rows if r["spread"] == spread], key=lambda r: -r["exp"])
        lines += [f"## Spread {spread} pts - best 15", "", hdr, sep]
        for r in sub[:15]:
            lines.append(f"| {r['spread']} | {r['tp']} | {r['sl']} | {r['rule']} | {r['n']} | {r['win']} | "
                         f"{r['avg_win']} | {r['avg_loss']} | {r['exp']} | {r['exp_first_half']} | "
                         f"{r['exp_second_half']} | {r['random_exp']} | {r['edge_vs_random']} | "
                         f"{r['worst_day_pts']} | {r['green_days']}/{len(names)} |")
        cur = [r for r in sub if r["tp"] == 5 and r["sl"] == 50 and r["rule"] == "none"][0]
        lines += ["", f"Current live settings (TP 5 / SL 50 / no early exit): exp {cur['exp']} pts, "
                  f"win {cur['win']}%, random {cur['random_exp']}", ""]
    open(os.path.join(OUT, "report.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    synthetic = "--synthetic" in sys.argv
    days = load_synthetic() if synthetic else load_real()
    names, n_sig, rows = run(days)
    report(names, n_sig, rows, "synthetic test data" if synthetic else "NQ=F 1-minute, Yahoo Finance")
