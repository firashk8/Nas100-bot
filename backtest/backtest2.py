"""
Round 2: does anything beat costs on real US100 data?
  A) same EMA9+VWAP signal on 1-min vs 5-min candles
  B) bigger targets (20/40/80 pts) and a trailing stop
  C) the opening-range breakout (standard mode's first trade)
All compared with random-direction entries at the same moments, both halves of the sample.
"""
import json
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest import load_real, load_synthetic, stats, SL_SLIP  # noqa: E402
from bot import Candle, aggressive_signal, orb_direction  # noqa: E402

OUT = os.environ.get("OUT_DIR", "results")
SPREAD = 1.5


def aggregate(bars, tf):
    out, cur = [], None
    for b in bars:
        hh, mm = map(int, b.t.split(":"))
        key = f"{hh:02d}:{mm - mm % tf:02d}"
        if cur is None or cur.t != key:
            if cur:
                out.append(cur)
            cur = Candle(t=key, open=b.open, high=b.high, low=b.low, close=b.close, ticks=1, vol=b.vol)
        else:
            cur.high, cur.low, cur.close = max(cur.high, b.high), min(cur.low, b.low), b.close
            cur.vol += b.vol
    if cur:
        out.append(cur)
    return out


def signals(bars, tf):
    out = {}
    for i in range(len(bars) - 1):
        t = bars[i].t
        mm = int(t[3:])
        if not ("09:34" <= t <= "14:44") or (mm % tf) != tf - 1:
            continue
        agg = aggregate(bars[: i + 1], tf) if tf > 1 else bars[: i + 1]
        s = aggressive_signal(agg)
        if s:
            out[i] = s
    return out


def sim(bars, sigs, tp, sl, trail, rnd=None, spread=SPREAD):
    half = spread / 2
    n, i, res = len(bars), 0, []
    while i < n - 1:
        if i not in sigs:
            i += 1
            continue
        side = sigs[i] if rnd is None else rnd.choice(["buy", "sell"])
        sg = 1 if side == "buy" else -1
        j = i + 1
        entry = bars[j].open + sg * half
        stop = entry - sg * sl              # exit-side price levels
        target = entry + sg * tp
        best = 0.0
        k, r = j, None
        while k < n:
            b = bars[k]
            if b.t >= "15:00":
                r = sg * (b.open - sg * half - entry)
                break
            lo_x, hi_x = b.low - half, b.high - half          # exit-side prices for a buy
            if side == "sell":
                lo_x, hi_x = b.low + half, b.high + half
            adverse = lo_x if side == "buy" else hi_x
            favour = hi_x if side == "buy" else lo_x
            if (adverse <= stop) if side == "buy" else (adverse >= stop):
                r = sg * (stop - entry) - SL_SLIP
                break
            if (favour >= target) if side == "buy" else (favour <= target):
                r = tp
                break
            if trail:
                best = max(best, sg * (favour - entry))
                if best >= trail:
                    new_stop = entry + sg * (best - trail)
                    stop = max(stop, new_stop) if side == "buy" else min(stop, new_stop)
            k += 1
        if r is None:
            r = sg * (bars[-1].close - sg * half - entry)
            k = n - 1
        res.append(r)
        i = k + 1
    return res


def orb(days, rnd=None, target_r=10.0, spread=SPREAD):
    res = []
    half = spread / 2
    for d, bars in days.items():
        first = [b for b in bars if "09:30" <= b.t < "09:35"]
        if len(first) < 5:
            continue
        c = Candle(open=first[0].open, high=max(b.high for b in first), low=min(b.low for b in first),
                   close=first[-1].close, ticks=60)
        side = orb_direction(c)
        if side is None:
            continue
        if rnd:
            side = rnd.choice(["buy", "sell"])
        sg = 1 if side == "buy" else -1
        rest = [b for b in bars if b.t >= "09:35"]
        entry = rest[0].open + sg * half
        stop = c.low if side == "buy" else c.high
        risk = sg * (entry - stop)
        if risk < 2 * (spread + 2):
            continue
        target = entry + sg * target_r * risk
        r = None
        for b in rest:
            if b.t >= "15:55":
                r = sg * (b.open - sg * half - entry)
                break
            adverse = (b.low - half) if side == "buy" else (b.high + half)
            favour = (b.high - half) if side == "buy" else (b.low + half)
            if (adverse <= stop) if side == "buy" else (adverse >= stop):
                r = -risk - SL_SLIP
                break
            if (favour >= target) if side == "buy" else (favour <= target):
                r = target_r * risk
                break
        if r is None:
            r = sg * (rest[-1].close - sg * half - entry)
        res.append(r / risk)          # in R units
    return res


def main():
    synthetic = "--synthetic" in sys.argv
    days = load_synthetic() if synthetic else load_real()
    names = sorted(days)
    half = len(names) // 2
    rows = []
    for tf in (1, 5):
        sigs = {d: signals(days[d], tf) for d in names}
        for tp in (5, 20, 40, 80):
            for sl in (15, 25, 50):
                for trail in (0, 15, 25):
                    if trail and trail >= tp:
                        continue
                    per = {d: sim(days[d], sigs[d], tp, sl, trail) for d in names}
                    allp = [p for d in names for p in per[d]]
                    rexp = []
                    for sd in range(10):
                        r = random.Random(sd)
                        rp = [p for d in names for p in sim(days[d], sigs[d], tp, sl, trail, r)]
                        rexp.append(statistics.mean(rp) if rp else 0)
                    st = stats(allp)
                    st.update(tf=tf, tp=tp, sl=sl, trail=trail,
                              h1=stats([p for d in names[:half] for p in per[d]])["exp"],
                              h2=stats([p for d in names[half:] for p in per[d]])["exp"],
                              rnd=round(statistics.mean(rexp), 3),
                              green=sum(1 for d in names if sum(per[d]) > 0))
                    st["edge"] = round(st["exp"] - st["rnd"], 3)
                    rows.append(st)
    orb_r = orb(days)
    orb_rnd = [statistics.mean(orb(days, random.Random(s)) or [0]) for s in range(20)]
    lines = [f"# Round 2 ({'synthetic' if synthetic else 'NQ=F real 1-min'}), spread {SPREAD} pts, "
             f"{len(names)} sessions {names[0]}..{names[-1]}", "",
             "| tf | TP | SL | trail | trades | win% | avg win | avg loss | exp pts | 1st half | 2nd half | random | edge | green days |",
             "|" + "---|" * 14]
    for r in sorted(rows, key=lambda r: -r["exp"])[:25]:
        lines.append(f"| {r['tf']}m | {r['tp']} | {r['sl']} | {r['trail'] or '-'} | {r['n']} | {r['win']} | "
                     f"{r['avg_win']} | {r['avg_loss']} | {r['exp']} | {r['h1']} | {r['h2']} | {r['rnd']} | "
                     f"{r['edge']} | {r['green']}/{len(names)} |")
    os_ = stats(orb_r)
    lines += ["", f"ORB (standard mode first trade, 10R target): {os_['n']} trades, win {os_['win']}%, "
              f"avg {os_['exp']} R per trade, total {os_['total']} R; random direction avg "
              f"{round(statistics.mean(orb_rnd), 3)} R"]
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "report2.md"), "w").write("\n".join(lines))
    json.dump({"rows": rows, "orb": orb_r}, open(os.path.join(OUT, "results2.json"), "w"))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
