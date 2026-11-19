"""
Open-straddle EA test (the "buy stop above / sell stop below at the NY open" robot).
At 09:29 close: buy stop at P + d, sell stop at P - d. First one triggered wins (OCO),
take profit TP, stop loss SL. If both orders trigger in the same 1-minute bar, or the
stop and target are both touched in one bar, the WORSE outcome is assumed.
Extra slippage on stop entries at the open: SLIP points. Flat at 10:30 if still open.
"""
import os
import random
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest import load_real, load_synthetic  # noqa: E402

OUT = os.environ.get("OUT_DIR", "results")
SPREAD = 1.5
SLIP = 2.0          # stop orders at the open usually fill worse than their price


def one_day(bars, d, tp, sl):
    pre = [b for b in bars if b.t == "09:29"]
    after = [b for b in bars if "09:30" <= b.t <= "10:30"]
    if not pre or not after:
        return None
    p = pre[0].close
    up, dn = p + d, p - d
    side, entry = None, None
    for i, b in enumerate(after):
        if side is None:
            hit_up, hit_dn = b.high >= up, b.low <= dn
            if hit_up and hit_dn:
                return -sl - SPREAD - SLIP           # whipsaw: both filled, assume a loss
            if hit_up:
                side, entry = "buy", max(up, b.open) + SPREAD / 2 + SLIP
            elif hit_dn:
                side, entry = "sell", min(dn, b.open) - SPREAD / 2 - SLIP
            else:
                continue
            # same bar after the fill: worst case first
            if side == "buy":
                if b.low - SPREAD / 2 <= entry - sl:
                    return -sl
                if b.high - SPREAD / 2 >= entry + tp:
                    return tp
            else:
                if b.high + SPREAD / 2 >= entry + sl:
                    return -sl
                if b.low + SPREAD / 2 <= entry - tp:
                    return tp
            continue
        if side == "buy":
            if b.low - SPREAD / 2 <= entry - sl:
                return -sl
            if b.high - SPREAD / 2 >= entry + tp:
                return tp
        else:
            if b.high + SPREAD / 2 >= entry + sl:
                return -sl
            if b.low + SPREAD / 2 <= entry - tp:
                return tp
    if side is None:
        return 0.0                                    # nothing triggered, orders cancelled
    last = after[-1].close
    return (last - SPREAD / 2 - entry) if side == "buy" else (entry - last - SPREAD / 2)


def main():
    days = load_synthetic() if "--synthetic" in sys.argv else load_real()
    names = sorted(days)
    lines = [f"# Open straddle on US100 (NQ=F 1-min), {len(names)} sessions {names[0]}..{names[-1]}",
             f"Spread {SPREAD}, extra entry slippage {SLIP} pts. Worst case assumed inside a bar.", "",
             "| distance | TP | SL | days traded | win% | total pts | avg pts/day | worst day | best day |",
             "|---|---|---|---|---|---|---|---|---|"]
    rows = []
    for d in (5, 10, 20, 40):
        for tp in (10, 20, 40, 80):
            for sl in (10, 20, 40):
                res = [one_day(days[n], d, tp, sl) for n in names]
                res = [r for r in res if r is not None]
                traded = [r for r in res if r != 0]
                if not traded:
                    continue
                rows.append((sum(res), d, tp, sl, len(traded),
                             round(100 * sum(1 for r in traded if r > 0) / len(traded), 1),
                             round(sum(res), 1), round(statistics.mean(res), 2),
                             round(min(res), 1), round(max(res), 1)))
    for r in sorted(rows, reverse=True)[:20]:
        lines.append("| " + " | ".join(str(x) for x in r[1:]) + " |")
    lines.append("")
    lines.append(f"Combinations tested: {len(rows)}, profitable: {sum(1 for r in rows if r[0] > 0)}")
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "straddle.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
