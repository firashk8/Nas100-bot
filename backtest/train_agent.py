"""
Train the prediction agent on real US100 data, honestly:
 1. replay the real bot (Stack mode) day by day on real NQ 1-minute prices with the filters OFF,
    recording a market snapshot + the result of every basket (bot.JOURNAL);
 2. walk-forward: train a small logistic model on the first 60% of days, test on the last 40%
    it has never seen; compare "take every basket" vs "take only what the agent approves";
 3. save agent_model.json, enabled only if the agent improved the unseen days.
"""
import asyncio
import json
import math
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot  # noqa: E402
import replay  # noqa: E402
from backtest import load_real  # noqa: E402

OUT = os.environ.get("OUT_DIR", "results")
F = bot.AGENT_FEATURES


def collect(days):
    names = sorted(days)

    async def fast_sleep(x):
        replay.Clock.t += timedelta(seconds=x if x else 1)

    bot.asyncio.sleep = fast_sleep
    bot.now_ny = lambda: replay.Clock.t
    replay.ReplayBroker.balance = 1000.0
    for d in names:
        bars = days[d]
        y, mo, dd = map(int, d.split("-"))
        replay.ReplayBroker.prices = {}
        for c in bars:
            hh, mm = map(int, c.t.split(":"))
            base = datetime(y, mo, dd, hh, mm, tzinfo=bot.NY)
            for k, px in enumerate(replay.second_path(c)):
                replay.ReplayBroker.prices[base + timedelta(seconds=k)] = px
        replay.Clock.t = datetime(y, mo, dd, 9, 12, tzinfo=bot.NY)
        s = bot.Settings(token="x", account_id="y", run_source="schedule")
        s.regime_filter = s.mtf_confirm = s.self_learning = s.agent = False
        s.stack_trend_filter = False
        s.daily_loss_percent = 100.0
        replay.ReplayBroker.balance = 1000.0          # same size every day: clean labels
        asyncio.run(bot.run_session(s, replay.ReplayBroker, replay.MemGH("stack"),
                                    clock=lambda: replay.Clock.t,
                                    noise_fetcher=lambda lb: (_ for _ in ()).throw(RuntimeError("off"))))
    return list(bot.JOURNAL)


def fit(rows, l2=1.0, iters=3000, lr=0.1):
    mu = {k: sum(r[k] for r in rows) / len(rows) for k in F}
    sd = {k: (sum((r[k] - mu[k]) ** 2 for r in rows) / len(rows)) ** 0.5 or 1.0 for k in F}
    X = [[(r[k] - mu[k]) / sd[k] for k in F] for r in rows]
    y = [r["win"] for r in rows]
    # weight each basket by how much money it made/lost: big losses matter most
    mabs = sum(abs(r["pl"]) for r in rows) / len(rows) or 1.0
    sw = [max(abs(r["pl"]) / mabs, 0.05) for r in rows]
    w, b = [0.0] * len(F), 0.0
    n = sum(sw)
    for _ in range(iters):
        gw, gb = [l2 * wi / n for wi in w], 0.0
        for xi, yi, si in zip(X, y, sw):
            z = max(-30, min(30, b + sum(a * c for a, c in zip(w, xi))))
            e = (1 / (1 + math.exp(-z)) - yi) * si
            gb += e / n
            for j in range(len(F)):
                gw[j] += e * xi[j] / n
        w = [wi - lr * g for wi, g in zip(w, gw)]
        b -= lr * gb
    return {"w": dict(zip(F, w)), "b": b, "mu": mu, "sd": sd, "n": len(rows)}


def evaluate(model, rows, thr):
    take = [r for r in rows if bot.agent_predict(model, r) >= thr]
    def s(x):
        return (len(x), round(100 * sum(r["win"] for r in x) / len(x), 1) if x else 0, round(sum(r["pl"] for r in x), 2))
    return s(rows), s(take)


def main():
    days = load_real()
    rows = collect(days)
    names = sorted({r["day"] for r in rows})
    cut = names[int(len(names) * 0.6)] if names else ""
    train = [r for r in rows if r["day"] < cut]
    test = [r for r in rows if r["day"] >= cut]
    lines = [f"# Prediction agent - walk-forward test on real US100 data",
             f"{len(rows)} baskets over {len(names)} days. Train: {len(train)} baskets (days < {cut}). "
             f"Test (never seen): {len(test)} baskets.", "",
             "| threshold | test: all baskets (n / win% / $) | test: agent-approved (n / win% / $) |",
             "|---|---|---|"]
    best = None
    if len(train) >= 30 and test:
        m = fit(train)
        # threshold chosen on TRAINING days only (no peeking at the test days)
        thr_best, pl_best = 0.0, None
        for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            _, tr = evaluate(m, train, thr)
            if tr[0] >= 10 and (pl_best is None or tr[2] > pl_best):
                thr_best, pl_best = thr, tr[2]
        for thr in (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
            a, b = evaluate(m, test, thr)
            lines.append(f"| {thr}{' (chosen on training days)' if thr == thr_best else ''} | {a} | {b} |")
            if thr == thr_best:
                best = (thr, b, a)
        lines += ["", "Weights (positive = more likely to win): " +
                  ", ".join(f"{k} {v:+.2f}" for k, v in sorted(m['w'].items(), key=lambda kv: -abs(kv[1])))]
    enabled = bool(best and best[1][0] >= 5 and best[1][2] > best[2][2] and best[1][2] >= 0)
    final = fit(rows) if len(rows) >= 30 else {"w": {}, "b": 0, "mu": {}, "sd": {}, "n": len(rows)}
    final["enabled"] = enabled
    final["threshold"] = best[0] if best else 0.55
    lines += ["", f"Agent {'ENABLED' if enabled else 'NOT enabled'}: "
              + (f"on unseen days it turned {best[2][2]:+.2f} into {best[1][2]:+.2f} at threshold {best[0]}"
                 if best else "not enough data")]
    os.makedirs(OUT, exist_ok=True)
    json.dump(final, open(os.path.join(OUT, "agent_model.json"), "w"), indent=1)
    json.dump(rows, open(os.path.join(OUT, "agent_journal.json"), "w"))
    open(os.path.join(OUT, "agent.md"), "w").write("\n".join(lines))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
