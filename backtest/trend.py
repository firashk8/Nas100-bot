"""
Trend-following research on US100 (NQ=F): "Noise boundary" intraday momentum
(Zarattini, Aziz & Barbon 2024, "Beat the Market"), adapted to Nasdaq-100.

Rule (per session, New York time):
  * open_px = session open, sigma(t) = average |close(t)/open - 1| at the same time of day
    over the previous 14 sessions  -> the "noise area"
  * upper = max(open, prev_close) * (1 + m*sigma), lower = min(open, prev_close) * (1 - m*sigma)
  * at each check time: close > upper -> LONG, close < lower -> SHORT
  * trailing exit: long exits if close < max(upper, VWAP); short if close > min(lower, VWAP)
  * flat before the close. Costs: spread + slippage on every entry/exit.
Compared with random direction at the same moments and with "always long" intraday.
Datasets: 1h bars ~2 years (checks hourly), 30m bars ~60 days, 5m bars ~60 days.
"""
import json
import os
import random
import statistics
import sys
from datetime import time

OUT = os.environ.get("OUT_DIR", "results")
COST = 1.5 / 2 + 0.5          # half spread + slippage, in points, per side


def load(interval, period):
    import yfinance as yf
    df = yf.download("NQ=F", interval=interval, period=period, progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df[~df.index.duplicated()].sort_index()
    df.index = df.index.tz_convert("America/New_York")
    days = {}
    for ts, r in df.iterrows():
        if ts.weekday() >= 5:
            continue
        days.setdefault(ts.date().isoformat(), []).append(
            (ts.time(), float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]),
             float(r["Volume"]) if r["Volume"] == r["Volume"] else 0.0))
    return days


def sessions(days, first, last_start):
    """Keep bars that START between first and last_start (inclusive)."""
    out = {}
    for d, bars in sorted(days.items()):
        b = [x for x in bars if first <= x[0] <= last_start]
        if len(b) >= 3:
            out[d] = b
    n = statistics.median(len(b) for b in out.values()) if out else 0
    return {d: b for d, b in out.items() if len(b) == n}


def run_noise(sess, m=1.0, lookback=14, check_every=1, rnd=None, always_long=False):
    names = sorted(sess)
    trades, daily = [], {}
    prev_close = None
    for di, d in enumerate(names):
        bars = sess[d]
        o = bars[0][1]
        if di < lookback or prev_close is None:
            prev_close = bars[-1][4]
            continue
        hist = [sess[names[j]] for j in range(di - lookback, di)]
        sig = [statistics.mean(abs(h[k][4] / h[0][1] - 1) for h in hist) for k in range(len(bars))]
        pv = vv = 0.0
        pos, entry, day_pts = 0, 0.0, 0.0
        for k, (t, bo, bh, bl, bc, vol) in enumerate(bars):
            w = vol if vol > 0 else 1.0
            pv += (bh + bl + bc) / 3 * w
            vv += w
            vwap = pv / vv
            ub = max(o, prev_close) * (1 + m * sig[k])
            lb = min(o, prev_close) * (1 - m * sig[k])
            last = k == len(bars) - 1
            if always_long:
                if k == 0:
                    pos, entry = 1, bo + COST
                if last:
                    r = bc - COST - entry
                    trades.append(r)
                    day_pts += r
                    pos = 0
                continue
            if pos == 1 and (bc < max(ub, vwap) or last):
                r = bc - COST - entry
                trades.append(r)
                day_pts += r
                pos = 0
            elif pos == -1 and (bc > min(lb, vwap) or last):
                r = entry - (bc + COST)
                trades.append(r)
                day_pts += r
                pos = 0
            if pos == 0 and not last and k % check_every == 0 and k > 0:
                want = 1 if bc > ub else -1 if bc < lb else 0
                if want and rnd is not None:
                    want = rnd.choice([1, -1])
                if want == 1:
                    pos, entry = 1, bc + COST
                elif want == -1:
                    pos, entry = -1, bc - COST
        daily[d] = day_pts
        prev_close = bars[-1][4]
    return trades, daily


def summarize(trades, daily):
    if not trades:
        return {"trades": 0}
    days = sorted(daily)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for d in days:
        eq += daily[d]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    by_year = {}
    for d in days:
        by_year[d[:4]] = round(by_year.get(d[:4], 0) + daily[d], 1)
    half = len(days) // 2
    wins = [t for t in trades if t > 0]
    return {"trades": len(trades), "days": len(days),
            "win%": round(100 * len(wins) / len(trades), 1),
            "avg_win": round(statistics.mean(wins), 1) if wins else 0,
            "avg_loss": round(statistics.mean([t for t in trades if t <= 0]), 1) if len(wins) < len(trades) else 0,
            "pts_per_trade": round(statistics.mean(trades), 2),
            "total_pts": round(sum(trades), 1),
            "first_half_pts": round(sum(daily[d] for d in days[:half]), 1),
            "second_half_pts": round(sum(daily[d] for d in days[half:]), 1),
            "green_days%": round(100 * sum(1 for d in days if daily[d] > 0) / max(1, sum(1 for d in days if daily[d] != 0)), 1),
            "worst_day": round(min(daily.values()), 1), "best_day": round(max(daily.values()), 1),
            "max_drawdown_pts": round(mdd, 1), "by_year": by_year}


def main():
    lines = ["# Trend-following (noise boundary) on US100 / NQ=F", "",
             f"Costs per side: {COST} pts (spread 1.5 round trip + 0.5 slippage each side).", ""]
    results = {}
    setups = [("1h", "730d", time(10, 0), time(15, 0), "1h bars, ~2 years (session from 10:00)"),
              ("30m", "60d", time(9, 30), time(15, 30), "30m bars, ~60 days"),
              ("5m", "60d", time(9, 30), time(15, 55), "5m bars, ~60 days (checks every 30 min)")]
    for interval, period, first, last, label in setups:
        try:
            sess = sessions(load(interval, period), first, last)
        except Exception as e:
            lines.append(f"{label}: download failed {e}")
            continue
        names = sorted(sess)
        if not names:
            lines.append(f"{label}: no data")
            continue
        check = 6 if interval == "5m" else 1
        lines += [f"## {label}: {len(names)} sessions {names[0]} .. {names[-1]}", ""]
        rows = []
        for m in (0.75, 1.0, 1.5):
            tr, dl = run_noise(sess, m=m, check_every=check)
            s = summarize(tr, dl)
            rnd_tot = []
            for sd in range(20):
                rt, _ = run_noise(sess, m=m, check_every=check, rnd=random.Random(sd))
                rnd_tot.append(sum(rt))
            s["random_total_pts"] = round(statistics.mean(rnd_tot), 1)
            s["random_beaten_%"] = round(100 * sum(1 for x in rnd_tot if s.get("total_pts", 0) > x) / len(rnd_tot))
            s["m"] = m
            rows.append(s)
        tl, dl = run_noise(sess, always_long=True)
        bh = summarize(tl, dl)
        lines.append("| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |")
        lines.append("|" + "---|" * 14)
        for s in rows:
            if not s.get("trades"):
                continue
            lines.append(f"| {s['m']} | {s['trades']} | {s['win%']} | {s['avg_win']} | {s['avg_loss']} | "
                         f"{s['pts_per_trade']} | {s['total_pts']} | {s['first_half_pts']} | {s['second_half_pts']} | "
                         f"{s['green_days%']} | {s['worst_day']} | {s['max_drawdown_pts']} | {s['random_total_pts']} | "
                         f"{s['random_beaten_%']}% |")
        lines += ["", f"Always-long intraday (open->close every day): total {bh.get('total_pts')} pts, "
                  f"max DD {bh.get('max_drawdown_pts')}", ""]
        for s in rows:
            if s.get("by_year"):
                lines.append(f"band {s['m']} by year: {s['by_year']}")
        lines.append("")
        results[interval] = {"rows": rows, "always_long": bh}
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "trend.md"), "w").write("\n".join(lines))
    json.dump(results, open(os.path.join(OUT, "trend.json"), "w"), indent=1, default=str)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
