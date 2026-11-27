#!/usr/bin/env python3
"""
GRID BOT - a faithful rebuild of the stacking bot in the video.

WHAT THE VIDEO SHOWED (velvettrade776, 13 Nov):
  MT5 on a phone, XAUUSD.m. Three sells of 0.02 lots stacked within $1 of each other,
  then four buys of 0.01-0.03 lots stacked within $1, each showing $1-7 profit.
  No stop loss visible on any of them. Balance $329 / $661 / $760 in different clips.

WHAT THIS DOES, IDENTICALLY:
  - opens a position in one direction
  - when price moves GRID_STEP_PCT against it, opens another in the SAME direction
  - each leg carries its own small take-profit and closes on its own
  - no stop loss by default, exactly as filmed (BASKET_STOP_PCT=0 turns it off, and 0 is the default)

HONEST DIFFERENCES I COULD NOT AVOID, AND WHY:
  1. No gold. A free API that trades real XAUUSD does not exist. Alpaca's free paper
     account covers US stocks, ETFs and crypto. For gold exposure use SYMBOL=GLD or IAU
     (gold ETFs, US hours only). For something trading right now, use a crypto pair.
  2. MT5 holds 4 separate positions; Alpaca nets them into one. So this bot keeps its own
     ledger of legs and closes them one at a time. Same behaviour, different bookkeeping.
  3. Paper account. Real prices, real spreads, simulated fills. No money at risk.

WHAT I MEASURED BEFORE BUILDING IT, so it is written down somewhere you can find it:
  Two years of hourly gold, 11,463 bars. For a position opened at a random hour and held
  up to five days, the move AGAINST it was:
      median $84, 75th pct $157, 90th pct $232, worst $682   (against a sell)
      median $58, 75th pct $137, 90th pct $241, worst $935   (against a buy)
  67.8% of entries saw gold run more than $50 against a sell; 44.2% more than $100.
  At the video's 0.06 lots that median $84 is $504 - against a $329 balance.
  And the same structure on your own NAS100 data gave 86.5% wins with the account down 44%.
  None of that is a reason not to run it on paper. It is the reason it runs on paper.

RUN IT:
  export ALPACA_KEY=...  ALPACA_SECRET=...
  python3 grid_bot.py
Stdlib only. No pip install.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------- settings
def env(name, default):
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default

KEY = env("ALPACA_KEY", "")
SECRET = env("ALPACA_SECRET", "")
PAPER = env("ALPACA_PAPER", "true").lower() != "false"

SYMBOL = env("SYMBOL", "BTC/USD")      # BTC/USD, ETH/USD (24/7) or GLD, IAU, QQQ (US hours)
DIRECTION = env("DIRECTION", "momentum")  # momentum | buy | sell
NOTIONAL = float(env("NOTIONAL", "500"))  # dollars per leg
MAX_LEGS = int(env("MAX_LEGS", "4"))      # the video showed up to 4
GRID_STEP_PCT = float(env("GRID_STEP_PCT", "0.05"))   # add a leg after this % against
TAKE_PROFIT_PCT = float(env("TAKE_PROFIT_PCT", "0.08"))  # each leg's own target, %
BASKET_STOP_PCT = float(env("BASKET_STOP_PCT", "0"))  # 0 = no stop, as filmed
BASKET_TP_PCT = float(env("BASKET_TP_PCT", "0"))      # 0 = legs close individually
POLL_SECONDS = float(env("POLL_SECONDS", "3"))
RUN_MINUTES = float(env("RUN_MINUTES", "330"))        # GitHub Actions caps a job at ~6h
STATUS_FILE = env("STATUS_FILE", "grid_status.json")
DRY_RUN = env("DRY_RUN", "false").lower() == "true"

TRADE_HOST = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"
IS_CRYPTO = "/" in SYMBOL

HEAD = {
    "APCA-API-KEY-ID": KEY,
    "APCA-API-SECRET-KEY": SECRET,
    "Content-Type": "application/json",
    "accept": "application/json",
}


GH_TOKEN = env("GITHUB_TOKEN", "")
GH_REPO = env("GITHUB_REPOSITORY", "")
STATUS_BRANCH = env("STATUS_BRANCH", "status")
STATUS_PATH = env("STATUS_PATH", "grid_status.json")


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def gh(method, path, body=None):
    url = f"https://api.github.com/repos/{GH_REPO}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "grid-bot",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


_sha = {"v": None}


def publish(payload):
    """Push live status to the `status` branch so the dashboard can read it."""
    if not GH_TOKEN or not GH_REPO:
        return
    try:
        try:
            gh("GET", f"/git/ref/heads/{STATUS_BRANCH}")
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            base = gh("GET", f"/git/ref/heads/{gh('GET', '')['default_branch']}")
            gh("POST", "/git/refs", {"ref": f"refs/heads/{STATUS_BRANCH}",
                                     "sha": base["object"]["sha"]})
        if _sha["v"] is None:
            try:
                _sha["v"] = gh("GET", f"/contents/{STATUS_PATH}?ref={STATUS_BRANCH}")["sha"]
            except Exception:
                _sha["v"] = ""
        body = {
            "message": "grid status",
            "content": base64.b64encode(json.dumps(payload, indent=1).encode()).decode(),
            "branch": STATUS_BRANCH,
        }
        if _sha["v"]:
            body["sha"] = _sha["v"]
        _sha["v"] = gh("PUT", f"/contents/{STATUS_PATH}", body)["content"]["sha"]
    except Exception as e:
        log(f"status publish failed: {str(e)[:120]}")
        _sha["v"] = None


def read_control():
    """Let the dashboard stop the bot mid-run by flipping grid_control.json."""
    if not GH_TOKEN or not GH_REPO:
        return {}
    try:
        r = gh("GET", f"/contents/grid_control.json?ref={STATUS_BRANCH}")
        return json.loads(base64.b64decode(r["content"]).decode())
    except Exception:
        return {}


def call(method, url, body=None, tries=3):
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(tries):
        req = urllib.request.Request(url, data=data, headers=HEAD, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:300]
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code} {detail}")
        except Exception as e:
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e}")


# ----------------------------------------------------------------- market data
def price():
    """Mid price. Real-time for crypto; IEX real-time for stocks on the free plan."""
    if IS_CRYPTO:
        q = urllib.parse.quote(SYMBOL, safe="")
        d = call("GET", f"{DATA_HOST}/v1beta3/crypto/us/latest/quotes?symbols={q}")
        book = d.get("quotes", {}).get(SYMBOL)
        if not book:
            raise RuntimeError(f"no crypto quote for {SYMBOL}: {d}")
        bid, ask = float(book["bp"]), float(book["ap"])
    else:
        d = call("GET", f"{DATA_HOST}/v2/stocks/{SYMBOL}/quotes/latest?feed=iex")
        book = d.get("quote") or {}
        bid, ask = float(book.get("bp", 0)), float(book.get("ap", 0))
        if bid <= 0 or ask <= 0:
            t = call("GET", f"{DATA_HOST}/v2/stocks/{SYMBOL}/trades/latest?feed=iex")
            p = float(t.get("trade", {}).get("p", 0))
            if p <= 0:
                raise RuntimeError(f"no quote or trade for {SYMBOL}")
            bid = ask = p
    return (bid + ask) / 2.0, bid, ask


def recent_closes(n=20):
    """Last n one-minute closes, for the momentum direction rule."""
    if IS_CRYPTO:
        q = urllib.parse.quote(SYMBOL, safe="")
        d = call("GET", f"{DATA_HOST}/v1beta3/crypto/us/bars?symbols={q}&timeframe=1Min&limit={n}")
        bars = d.get("bars", {}).get(SYMBOL, [])
    else:
        d = call("GET", f"{DATA_HOST}/v2/stocks/{SYMBOL}/bars?timeframe=1Min&limit={n}&feed=iex")
        bars = d.get("bars", [])
    return [float(b["c"]) for b in bars]


def market_open():
    if IS_CRYPTO:
        return True
    return bool(call("GET", f"{TRADE_HOST}/v2/clock").get("is_open"))


def equity():
    return float(call("GET", f"{TRADE_HOST}/v2/account")["equity"])


# ----------------------------------------------------------------- orders
def submit(side, qty):
    body = {
        "symbol": SYMBOL,
        "side": side,
        "type": "market",
        "qty": str(qty),
        "time_in_force": "gtc" if IS_CRYPTO else "day",
    }
    if DRY_RUN:
        log(f"DRY_RUN would {side} {qty} {SYMBOL}")
        return {"id": "dry", "status": "dry"}
    return call("POST", f"{TRADE_HOST}/v2/orders", body)


def size_for(px):
    qty = NOTIONAL / px
    return round(qty, 6) if IS_CRYPTO else max(1, int(qty))


def buying_power():
    a = call("GET", f"{TRADE_HOST}/v2/account")
    return float(a.get("non_marginable_buying_power") if IS_CRYPTO else a.get("buying_power", 0))


_warned = {"short": False}


def resolve_side(side):
    """Alpaca has no short selling in crypto. A sell there can only close what you hold,
    so a crypto grid is long only. Stocks and ETFs can be shorted on a margin account."""
    if IS_CRYPTO and side == "sell":
        if not _warned["short"]:
            _warned["short"] = True
            log("NOTE: crypto cannot be shorted on Alpaca - this grid will trade long only. "
                "Use GLD, IAU or QQQ in US hours if you want it to short too.")
        return "buy"
    return side


def close_everything(legs, reason):
    if not legs:
        return
    total = sum(l["qty"] for l in legs)
    side = "sell" if legs[0]["side"] == "buy" else "buy"
    total = round(total, 6) if IS_CRYPTO else int(total)
    if total > 0:
        try:
            submit(side, total)
            log(f"CLOSED all {len(legs)} legs ({reason})")
        except Exception as e:
            log(f"could not close: {e}")
    legs.clear()


# ----------------------------------------------------------------- the grid
def main():
    if not KEY or not SECRET:
        log("ALPACA_KEY / ALPACA_SECRET are not set. Get them from alpaca.markets "
            "(Paper Only account) and put them in GitHub Secrets.")
        return 1

    acct_start = equity()
    log(f"grid bot up. {SYMBOL}  equity ${acct_start:,.2f}  paper={PAPER}")
    log(f"legs<={MAX_LEGS}  ${NOTIONAL}/leg  step {GRID_STEP_PCT}%  tp {TAKE_PROFIT_PCT}%  "
        f"basket stop {BASKET_STOP_PCT or 'OFF (as filmed)'}")

    legs = []           # each: {side, entry, qty, tp, n}
    realized = 0.0
    wins = 0
    losses = 0
    opened = 0
    deadline = time.time() + RUN_MINUTES * 60
    last_status = 0.0
    last_err = 0.0

    while time.time() < deadline:
        try:
            if not market_open():
                log(f"{SYMBOL} market closed - waiting")
                time.sleep(60)
                continue

            mid, bid, ask = price()

            # ---- each leg closes on its own, exactly like the video's rows
            still = []
            for leg in legs:
                px = bid if leg["side"] == "buy" else ask
                hit = px >= leg["tp"] if leg["side"] == "buy" else px <= leg["tp"]
                if hit:
                    gain = (px - leg["entry"]) * leg["qty"] * (1 if leg["side"] == "buy" else -1)
                    realized += gain
                    wins += 1
                    try:
                        submit("sell" if leg["side"] == "buy" else "buy", leg["qty"])
                        log(f"  TP leg {leg['n']} @ {px:.2f}  +${gain:.2f}   (total ${realized:+.2f})")
                    except Exception as e:
                        log(f"  leg {leg['n']} close failed: {e}")
                        still.append(leg)
                else:
                    still.append(leg)
            legs = still

            # ---- open the first leg
            if not legs:
                side = DIRECTION
                if DIRECTION == "momentum":
                    c = recent_closes(10)
                    side = "buy" if len(c) >= 3 and c[-1] >= c[0] else "sell"
                side = resolve_side(side)
                if buying_power() < NOTIONAL:
                    raise RuntimeError(
                        f"not enough buying power for a ${NOTIONAL:,.0f} leg "
                        f"(have ${buying_power():,.2f}). Lower '$ per leg' in the dashboard.")
                entry = ask if side == "buy" else bid
                qty = size_for(entry)
                tp = entry * (1 + TAKE_PROFIT_PCT / 100) if side == "buy" else entry * (1 - TAKE_PROFIT_PCT / 100)
                submit(side, qty)
                opened += 1
                legs.append({"side": side, "entry": entry, "qty": qty, "tp": tp, "n": opened})
                log(f"OPEN leg 1 {side} {qty} @ {entry:.2f}  tp {tp:.2f}")

            # ---- add a leg when price goes against the last one (this is the grid)
            elif len(legs) < MAX_LEGS:
                last = legs[-1]
                against = (last["entry"] - mid) / last["entry"] * 100 if last["side"] == "buy" \
                    else (mid - last["entry"]) / last["entry"] * 100
                if against >= GRID_STEP_PCT and buying_power() >= NOTIONAL:
                    side = last["side"]
                    entry = ask if side == "buy" else bid
                    qty = size_for(entry)
                    tp = entry * (1 + TAKE_PROFIT_PCT / 100) if side == "buy" else entry * (1 - TAKE_PROFIT_PCT / 100)
                    submit(side, qty)
                    opened += 1
                    legs.append({"side": side, "entry": entry, "qty": qty, "tp": tp, "n": opened})
                    log(f"ADD leg {len(legs)} {side} {qty} @ {entry:.2f}  "
                        f"(price {against:.2f}% against leg {last['n']})")

            # ---- floating P&L across the stack
            floating = 0.0
            for leg in legs:
                px = bid if leg["side"] == "buy" else ask
                floating += (px - leg["entry"]) * leg["qty"] * (1 if leg["side"] == "buy" else -1)

            if BASKET_TP_PCT > 0 and floating >= acct_start * BASKET_TP_PCT / 100:
                close_everything(legs, f"basket target +${floating:.2f}")
                realized += floating
                wins += 1
            elif BASKET_STOP_PCT > 0 and floating <= -acct_start * BASKET_STOP_PCT / 100:
                close_everything(legs, f"basket stop -${abs(floating):.2f}")
                realized += floating
                losses += 1

            # ---- status every 30s
            if time.time() - last_status > 30:
                last_status = time.time()
                worst = min((((bid if l['side'] == 'buy' else ask) - l['entry'])
                             * (1 if l['side'] == 'buy' else -1) / l['entry'] * 100)
                            for l in legs) if legs else 0.0
                log(f"{SYMBOL} {mid:.2f} | legs {len(legs)} | floating ${floating:+.2f} "
                    f"| worst leg {worst:+.2f}% | realized ${realized:+.2f} | {wins}W/{losses}L")
                eq_now = equity()
                snap = {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "state": "RUNNING",
                    "symbol": SYMBOL, "price": round(mid, 2), "bid": round(bid, 2), "ask": round(ask, 2),
                    "open_legs": len(legs), "max_legs": MAX_LEGS,
                    "floating": round(floating, 2), "realized": round(realized, 2),
                    "net": round(realized + floating, 2),
                    "worst_leg_pct": round(worst, 3),
                    "wins": wins, "losses": losses, "legs_opened": opened,
                    "win_rate": round(100.0 * wins / opened, 1) if opened else 0.0,
                    "equity": round(eq_now, 2), "start_equity": round(acct_start, 2),
                    "drawdown_pct": round(100.0 * (acct_start - eq_now) / acct_start, 2) if acct_start else 0.0,
                    "basket_stop_pct": BASKET_STOP_PCT,
                    "notional": NOTIONAL, "grid_step_pct": GRID_STEP_PCT,
                    "take_profit_pct": TAKE_PROFIT_PCT, "direction": DIRECTION,
                    "seconds_left": int(max(0, deadline - time.time())),
                    "legs": [{
                        "n": l["n"], "side": l["side"], "entry": round(l["entry"], 2),
                        "tp": round(l["tp"], 2), "qty": l["qty"],
                        "pnl": round((( bid if l["side"] == "buy" else ask) - l["entry"])
                                     * l["qty"] * (1 if l["side"] == "buy" else -1), 2),
                        "pct": round((((bid if l["side"] == "buy" else ask) - l["entry"])
                                      * (1 if l["side"] == "buy" else -1) / l["entry"] * 100), 3),
                    } for l in legs],
                }
                try:
                    with open(STATUS_FILE, "w") as f:
                        json.dump(snap, f, indent=2)
                except Exception:
                    pass
                publish(snap)

                ctl = read_control()
                if ctl.get("stop"):
                    close_everything(legs, "stopped from the dashboard")
                    realized += floating
                    snap["state"] = "STOPPED"
                    publish(snap)
                    log("stop requested from the dashboard - closing and exiting")
                    break
                if ctl.get("flatten"):
                    close_everything(legs, "flatten requested from the dashboard")
                    realized += floating

            time.sleep(POLL_SECONDS)

        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"error: {e}")
            # Put the failure on the dashboard instead of leaving it frozen and silent.
            if time.time() - last_err > 45:
                last_err = time.time()
                publish({
                    "time": datetime.now(timezone.utc).isoformat(),
                    "state": "ERROR", "error": str(e)[:400],
                    "symbol": SYMBOL, "direction": DIRECTION,
                    "notional": NOTIONAL, "max_legs": MAX_LEGS,
                    "open_legs": len(legs), "floating": 0.0, "realized": round(realized, 2),
                    "net": round(realized, 2), "worst_leg_pct": 0.0,
                    "wins": wins, "losses": losses, "legs_opened": opened,
                    "win_rate": round(100.0 * wins / opened, 1) if opened else 0.0,
                    "equity": acct_start, "start_equity": acct_start,
                    "drawdown_pct": 0.0, "basket_stop_pct": BASKET_STOP_PCT,
                    "grid_step_pct": GRID_STEP_PCT, "take_profit_pct": TAKE_PROFIT_PCT,
                    "seconds_left": int(max(0, deadline - time.time())), "legs": [],
                })
            time.sleep(10)

    floating = 0.0
    try:
        mid, bid, ask = price()
        for leg in legs:
            px = bid if leg["side"] == "buy" else ask
            floating += (px - leg["entry"]) * leg["qty"] * (1 if leg["side"] == "buy" else -1)
    except Exception:
        pass
    log("-" * 60)
    log(f"session over. closed {wins} legs in profit, {losses} basket stops.")
    log(f"realized ${realized:+.2f} | still open: {len(legs)} legs worth ${floating:+.2f}")
    log("Open legs stay open - that is what the bot in the video does too.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
