"""
"Strongest strategies in the world" on the Nasdaq-100 (^NDX daily since 1999, ~25 years).
All signals use information available at the close; trades happen afterwards. Costs: CFD
spread on every position change (COST_PTS) + overnight financing on every night held
(FIN_YR per year, like an Exness swap). Results split into two halves to check robustness.
Then loss-avoidance tactics are tested as overlays: trend filter, volatility targeting,
drawdown circuit breaker, and a diversified portfolio of the best strategies.
"""
import math
import os
import statistics

OUT = os.environ.get("OUT_DIR", "results")
COST_PCT = 0.0001        # 0.01% per side (~2 points at today's US100 level)
FIN_YR = 0.06           # 6%/year overnight financing on held positions (approx. CFD swap)


def load():
    import yfinance as yf
    df = yf.download("^NDX", start="1999-01-01", interval="1d", progress=False, auto_adjust=False)
    if hasattr(df.columns, "levels"):
        df.columns = [c[0] for c in df.columns]
    df = df.dropna()
    rows = [(d.date().isoformat(), float(r.Open), float(r.High), float(r.Low), float(r.Close))
            for d, r in df.iterrows()]
    return rows


def sma(x, n):
    out, s = [None] * len(x), 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def rsi(x, n=2):
    out = [None] * len(x)
    g = l = 0.0
    for i in range(1, len(x)):
        ch = x[i] - x[i - 1]
        up, dn = max(ch, 0), max(-ch, 0)
        if i <= n:
            g += up / n
            l += dn / n
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
        if i >= n:
            out[i] = 100 if l == 0 else 100 - 100 / (1 + g / l)
    return out


# ---------------------------------------------------------------- daily position strategies
# pos[i] = exposure held from close i to close i+1 (decided with data up to close i)
def strat_positions(rows):
    o = [r[1] for r in rows]; h = [r[2] for r in rows]; lo = [r[3] for r in rows]; c = [r[4] for r in rows]
    n = len(c)
    s200, s50, s5 = sma(c, 200), sma(c, 50), sma(c, 5)
    r2 = rsi(c, 2)
    P = {}
    P["Buy & hold"] = [1.0] * n
    P["Trend: price > 200-day MA"] = [1.0 if s200[i] and c[i] > s200[i] else 0.0 for i in range(n)]
    P["Trend: 50/200 golden cross"] = [1.0 if s200[i] and s50[i] > s200[i] else 0.0 for i in range(n)]
    # Turtle / Donchian 55-day breakout in, 20-day low out (long only)
    pos, d = [], 0.0
    for i in range(n):
        if i >= 55 and c[i] >= max(h[i - 55:i]):
            d = 1.0
        elif i >= 20 and c[i] <= min(lo[i - 20:i]):
            d = 0.0
        pos.append(d)
    P["Turtle: 55-day breakout"] = pos
    P["Momentum: 12-month return > 0"] = [1.0 if i >= 252 and c[i] > c[i - 252] else 0.0 for i in range(n)]
    # Connors RSI(2): buy RSI2<10 above 200MA, exit close > 5MA
    pos, d = [], 0.0
    for i in range(n):
        if d == 0 and r2[i] is not None and s200[i] and r2[i] < 10 and c[i] > s200[i]:
            d = 1.0
        elif d == 1 and s5[i] and c[i] > s5[i]:
            d = 0.0
        pos.append(d)
    P["Mean reversion: Connors RSI(2)"] = pos
    P["Mean reversion: IBS < 0.2"] = [1.0 if h[i] > lo[i] and (c[i] - lo[i]) / (h[i] - lo[i]) < 0.2 else 0.0
                                      for i in range(n)]
    P["Mean reversion: after -1% day"] = [1.0 if i and c[i] / c[i - 1] - 1 < -0.01 else 0.0 for i in range(n)]
    # Turn of month: last trading day of month to 3rd trading day of next
    mon = [r[0][:7] for r in rows]
    pos = []
    for i in range(n):
        last = i + 1 < n and mon[i + 1] != mon[i]
        k = 0
        j = i
        while j > 0 and mon[j - 1] == mon[i]:
            j -= 1
            k += 1
        pos.append(1.0 if last or (k < 2 and i + 1 < n and mon[i + 1] == mon[i]) else 0.0)
    P["Calendar: turn of the month"] = pos
    return P


def run_daily(rows, pos, scale=None):
    c = [r[4] for r in rows]
    eq, eqs, prev, trades, rets = 1.0, [], 0.0, 0, []
    for i in range(len(c) - 1):
        p = pos[i] * (scale[i] if scale else 1.0)
        r = p * (c[i + 1] / c[i] - 1)
        cost = abs(p - prev) * COST_PCT + abs(p) * FIN_YR / 252
        if abs(p - prev) > 1e-9 and p != 0:
            trades += 1
        eq *= 1 + r - cost
        rets.append(r - cost)
        eqs.append(eq)
        prev = p
    return eqs, rets, trades


def run_intraday(rows, kind):
    """Day-trades opened and closed the same day (no financing)."""
    out = []
    for i in range(1, len(rows)):
        d, o, h, lo, c = rows[i]
        pc = rows[i - 1][4]
        rng = rows[i - 1][2] - rows[i - 1][3]
        r = 0.0
        if kind == "Larry Williams breakout":
            lvl = o + 0.5 * rng
            if h >= lvl:
                r = (c - lvl) / lvl - 2 * COST_PCT
        elif kind == "Gap fade":
            g = o / pc - 1
            if g > 0.005:
                r = (o - c) / o - 2 * COST_PCT
            elif g < -0.005:
                r = (c - o) / o - 2 * COST_PCT
        elif kind == "Overnight only (close->open)":
            r = (o - pc) / pc - 2 * COST_PCT - FIN_YR / 252
        elif kind == "Day only (open->close)":
            r = (c - o) / o - 2 * COST_PCT
        out.append(r)
    eq, eqs, trades = 1.0, [], 0
    for r in out:
        eq *= 1 + r
        eqs.append(eq)
        trades += 1 if r != 0 else 0
    return eqs, out, trades


def metrics(eqs, rets, trades, years):
    if not eqs:
        return {}
    peak, mdd = 0, 0
    for e in eqs:
        peak = max(peak, e)
        mdd = min(mdd, e / peak - 1)
    cagr = eqs[-1] ** (1 / years) - 1
    sd = statistics.pstdev(rets) * math.sqrt(252)
    sharpe = (statistics.mean(rets) * 252) / sd if sd > 0 else 0
    active = [r for r in rets if r != 0]
    return {"cagr": cagr, "mdd": mdd, "sharpe": sharpe, "trades": trades,
            "win": (sum(1 for r in active if r > 0) / len(active)) if active else 0,
            "final": eqs[-1]}


def halves(rets):
    h = len(rets) // 2
    a = math.prod(1 + r for r in rets[:h]) - 1
    b = math.prod(1 + r for r in rets[h:]) - 1
    return a, b


def main():
    rows = load()
    years = len(rows) / 252
    P = strat_positions(rows)
    c = [r[4] for r in rows]
    res = {}
    for name, pos in P.items():
        eqs, rets, tr = run_daily(rows, pos)
        res[name] = (metrics(eqs, rets, tr, years), halves(rets), rets)
    for kind in ("Larry Williams breakout", "Gap fade", "Overnight only (close->open)", "Day only (open->close)"):
        eqs, rets, tr = run_intraday(rows, kind)
        res["Intraday: " + kind] = (metrics(eqs, rets, tr, years), halves(rets), rets)

    # ------------------------------------------------ loss-avoidance tactics (overlays)
    rv = [None] * len(c)
    for i in range(21, len(c)):
        rr = [c[j] / c[j - 1] - 1 for j in range(i - 19, i + 1)]
        rv[i] = statistics.pstdev(rr) * math.sqrt(252)
    vt = [min(2.0, 0.15 / rv[i]) if rv[i] else 0.0 for i in range(len(c))]
    s200 = sma(c, 200)
    tactics = {}
    base = P["Buy & hold"]
    eqs, rets, tr = run_daily(rows, base, vt)
    tactics["Buy & hold + volatility targeting (15%)"] = (metrics(eqs, rets, tr, years), halves(rets), rets)
    tf = P["Trend: price > 200-day MA"]
    eqs, rets, tr = run_daily(rows, tf, vt)
    tactics["200-day trend + volatility targeting"] = (metrics(eqs, rets, tr, years), halves(rets), rets)
    rsi_tf = P["Mean reversion: Connors RSI(2)"]
    # drawdown circuit breaker on buy & hold: go flat after -15% from peak until price > 200MA
    pos, peak, off = [], c[0], False
    for i in range(len(c)):
        peak = max(peak, c[i])
        if c[i] / peak - 1 < -0.15:
            off = True
        if off and s200[i] and c[i] > s200[i]:
            off, peak = False, c[i]
        pos.append(0.0 if off else 1.0)
    eqs, rets, tr = run_daily(rows, pos)
    tactics["Buy & hold + circuit breaker (-15%)"] = (metrics(eqs, rets, tr, years), halves(rets), rets)
    # portfolio: equal mix of trend, RSI2, turn-of-month (daily rebalanced returns)
    parts = [res["Trend: price > 200-day MA"][2], res["Mean reversion: Connors RSI(2)"][2],
             res["Calendar: turn of the month"][2]]
    mix = [sum(p[i] for p in parts) / len(parts) for i in range(len(parts[0]))]
    eq, eqs = 1.0, []
    for r in mix:
        eq *= 1 + r
        eqs.append(eq)
    tactics["Portfolio: trend + RSI(2) + turn-of-month"] = (metrics(eqs, mix, 0, years), halves(mix), mix)

    def table(d):
        L = ["| strategy | yearly return | worst drawdown | Sharpe | trades | win% | 1st half | 2nd half |",
             "|---|---|---|---|---|---|---|---|"]
        for name, (m, (a, b), _) in sorted(d.items(), key=lambda kv: -kv[1][0].get("sharpe", -9)):
            L.append(f"| {name} | {m['cagr']*100:+.1f}% | {m['mdd']*100:.0f}% | {m['sharpe']:.2f} | "
                     f"{m['trades']} | {m['win']*100:.0f}% | {a*100:+.0f}% | {b*100:+.0f}% |")
        return L
    lines = [f"# Famous strategies on the Nasdaq-100, {rows[0][0]} to {rows[-1][0]} ({years:.1f} years)",
             f"Costs: {COST_PCT*100:.2f}% per side, overnight financing {FIN_YR*100:.0f}%/year on held positions.",
             "Sorted by Sharpe ratio (return per unit of risk).", ""] + table(res) + \
            ["", "## Loss-avoidance tactics", ""] + table(tactics)
    os.makedirs(OUT, exist_ok=True)
    open(os.path.join(OUT, "world.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
