#!/usr/bin/env python3
"""
REVERSAL SCANNER - the only strategy in this repo that survived honest testing.

THE SETUP
  A symbol closes 1.5% or more BELOW its own open. Next session: buy at the
  opening auction, sell at the closing auction. Flat overnight. One day, one trade.

WHY THIS ONE AND NOT THE GRID
  Measured on 27 years of daily data with 2 bps of cost charged per round trip:

    QQQ  754 trades over 27.6y   55.0% win   5.30x growth   29.2% max drawdown
    IWM  663 trades over 26.4y   55.2% win   3.04x growth   27.7% max drawdown

  Both halves of the window positive on both symbols (QQQ 24.35 / 26.26 bps,
  IWM 17.82 / 19.49). On SPY the same setup returned 19.18 bps a trade over
  20 years. Three indices, independently, across three decades.

  For comparison the grid bot in this repo: 98% win rate, -59% account.

THE FREQUENCY PROBLEM, AND THE HONEST FIX
  One symbol triggers about 27 times a year. That is not enough to trade.
  The fix is NOT to loosen the threshold - at -1% the edge roughly halves and
  on IWM it decays to almost nothing in the recent era. The fix is MORE SYMBOLS.
  Thirty names trigger ~3 times a day, each at the full edge rather than a
  diluted one. Breadth, not leniency.

WHY IT NEEDS NO REAL-TIME DATA AND CANNOT BE HURT BY DELAYS
  The signal is known after yesterday's close. The entry is an opening-auction
  order and the exit is a closing-auction order, both filled at the official
  auction price. Nothing reacts to intraday price, so a slow feed costs nothing.
  This is the one strategy shape where free delayed data is not a handicap.

HOW IT RUNS - two short jobs a day, about a minute each:
  night (after 19:00 ET)  MODE=scan   finds yesterday's drops, queues buy-on-open
  morning (after 09:45 ET) MODE=exit  queues sell-on-close for whatever filled

Stdlib only. No pip install.
"""

import base64
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone


def env(name, default):
    v = os.environ.get(name, "")
    return v.strip() if v.strip() else default


KEY = env("ALPACA_KEY", "")
SECRET = env("ALPACA_SECRET", "")
PAPER = env("ALPACA_PAPER", "true").lower() != "false"
MODE = env("MODE", "scan").lower()                 # scan | exit | report

UNIVERSE = [s.strip().upper() for s in env(
    "UNIVERSE",
    "QQQ,SPY,IWM,DIA,XLK,XLF,XLE,XLV,SMH,ARKK,"
    "AAPL,MSFT,NVDA,AMZN,GOOGL,META,TSLA,AMD,NFLX,AVGO,"
    "CRM,ADBE,INTC,MU,QCOM,PLTR,COIN,UBER,BA,JPM"
).split(",") if s.strip()]

DROP_PCT = float(env("DROP_PCT", "1.5"))           # trigger: close this % below the open
RISK_PCT = float(env("RISK_PCT", "10"))            # % of equity per signal
MAX_POSITIONS = int(env("MAX_POSITIONS", "5"))     # most signals to take in one day
MIN_PRICE = float(env("MIN_PRICE", "5"))
DRY_RUN = env("DRY_RUN", "false").lower() == "true"

TRADE_HOST = "https://paper-api.alpaca.markets" if PAPER else "https://api.alpaca.markets"
DATA_HOST = "https://data.alpaca.markets"
HEAD = {"APCA-API-KEY-ID": KEY, "APCA-API-SECRET-KEY": SECRET,
        "Content-Type": "application/json", "accept": "application/json"}

GH_TOKEN = env("GITHUB_TOKEN", "")
GH_REPO = env("GITHUB_REPOSITORY", "")
STATUS_BRANCH = env("STATUS_BRANCH", "status")
STATUS_PATH = env("STATUS_PATH", "reversal_status.json")


def log(m):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def call(method, url, body=None, tries=3):
    data = json.dumps(body).encode() if body is not None else None
    for a in range(tries):
        try:
            req = urllib.request.Request(url, data=data, headers=HEAD, method=method)
            with urllib.request.urlopen(req, timeout=25) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:250]
            if e.code in (429, 500, 502, 503, 504) and a < tries - 1:
                time.sleep(2 * (a + 1))
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e.code} {detail}")
        except Exception as e:
            if a < tries - 1:
                time.sleep(2 * (a + 1))
                continue
            raise RuntimeError(f"{method} {url.split('?')[0]} -> {e}")


def gh(method, path, body=None):
    url = f"https://api.github.com/repos/{GH_REPO}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "reversal-bot"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read().decode()
        return json.loads(raw) if raw else {}


def publish(payload):
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
        sha = ""
        try:
            sha = gh("GET", f"/contents/{STATUS_PATH}?ref={STATUS_BRANCH}")["sha"]
        except Exception:
            pass
        body = {"message": "reversal status", "branch": STATUS_BRANCH,
                "content": base64.b64encode(json.dumps(payload, indent=1).encode()).decode()}
        if sha:
            body["sha"] = sha
        gh("PUT", f"/contents/{STATUS_PATH}", body)
    except Exception as e:
        log(f"status publish failed: {str(e)[:120]}")


# ------------------------------------------------------------------ data
def last_sessions(symbol, n=4):
    """Most recent completed daily bars. Delayed data is fine - we only need
    yesterday's open and close, which stopped changing when the bell rang."""
    url = (f"{DATA_HOST}/v2/stocks/{symbol}/bars"
           f"?timeframe=1Day&limit={n}&adjustment=split&feed=iex")
    return call("GET", url).get("bars") or []


def equity():
    return float(call("GET", f"{TRADE_HOST}/v2/account")["equity"])


def clock():
    return call("GET", f"{TRADE_HOST}/v2/clock")


def positions():
    return call("GET", f"{TRADE_HOST}/v2/positions") or []


def open_orders():
    return call("GET", f"{TRADE_HOST}/v2/orders?status=open&limit=200") or []


# ------------------------------------------------------------------ scan
def find_signals():
    hits, checked = [], 0
    for sym in UNIVERSE:
        try:
            bars = last_sessions(sym, 4)
            if len(bars) < 1:
                continue
            b = bars[-1]
            o, c = float(b["o"]), float(b["c"])
            if o <= 0 or c < MIN_PRICE:
                continue
            checked += 1
            move = (c / o - 1) * 100
            if move <= -DROP_PCT:
                hits.append({"symbol": sym, "date": b["t"][:10],
                             "open": round(o, 2), "close": round(c, 2),
                             "drop_pct": round(move, 2)})
        except Exception as e:
            log(f"  {sym}: {str(e)[:90]}")
    log(f"checked {checked}/{len(UNIVERSE)} symbols")
    return sorted(hits, key=lambda h: h["drop_pct"])      # biggest drop first


def do_scan():
    sigs = find_signals()
    if not sigs:
        log(f"no symbol closed {DROP_PCT}% below its open. nothing to do.")
        return sigs, []
    log(f"{len(sigs)} signal(s): " + ", ".join(f"{s['symbol']} {s['drop_pct']}%" for s in sigs))

    take = sigs[:MAX_POSITIONS]
    eq = equity()
    placed = []
    for s in take:
        notional = eq * RISK_PCT / 100
        qty = max(1, int(notional / s["close"]))
        body = {"symbol": s["symbol"], "side": "buy", "type": "market",
                "qty": str(qty), "time_in_force": "opg"}      # market-on-open
        if DRY_RUN:
            log(f"  DRY_RUN buy-on-open {qty} {s['symbol']}")
            placed.append(dict(s, qty=qty, order="dry"))
            continue
        try:
            o = call("POST", f"{TRADE_HOST}/v2/orders", body)
            log(f"  queued BUY-ON-OPEN {qty} {s['symbol']}  (dropped {s['drop_pct']}%)")
            placed.append(dict(s, qty=qty, order=o.get("id")))
        except Exception as e:
            log(f"  {s['symbol']} order rejected: {str(e)[:150]}")
    return sigs, placed


# ------------------------------------------------------------------ exit
def do_exit():
    """Queue a sell-on-close for everything we hold. Runs after the open, so the
    quantity is whatever actually filled - never more than the broker says we own."""
    pos = positions()
    if not pos:
        log("no positions to close.")
        return []
    done = []
    for p in pos:
        sym = p["symbol"]
        qty = abs(int(float(p.get("qty_available") or p.get("qty") or 0)))
        if qty <= 0:
            continue
        body = {"symbol": sym, "side": "sell", "type": "market",
                "qty": str(qty), "time_in_force": "cls"}      # market-on-close
        if DRY_RUN:
            log(f"  DRY_RUN sell-on-close {qty} {sym}")
            continue
        try:
            call("POST", f"{TRADE_HOST}/v2/orders", body)
            log(f"  queued SELL-ON-CLOSE {qty} {sym}  (unrealized ${float(p['unrealized_pl']):+.2f})")
            done.append({"symbol": sym, "qty": qty,
                         "unrealized": round(float(p["unrealized_pl"]), 2)})
        except Exception as e:
            log(f"  {sym} exit rejected: {str(e)[:150]}")
    return done


# ------------------------------------------------------------------ main
def main():
    if not KEY or not SECRET:
        log("ALPACA_KEY / ALPACA_SECRET not set.")
        return 1

    eq = equity()
    ck = clock()
    log(f"reversal scanner | mode={MODE} | equity ${eq:,.2f} | market "
        f"{'OPEN' if ck.get('is_open') else 'closed'} | universe {len(UNIVERSE)} symbols")
    log(f"trigger: close {DROP_PCT}% or more below the same day's open")

    sigs, placed, exits = [], [], []
    if MODE == "scan":
        sigs, placed = do_scan()
    elif MODE == "exit":
        exits = do_exit()
    else:
        sigs = find_signals()
        log(json.dumps(sigs, indent=1))

    pos = positions()
    snap = {
        "time": datetime.now(timezone.utc).isoformat(),
        "mode": MODE, "state": "OK",
        "equity": round(eq, 2),
        "market_open": bool(ck.get("is_open")),
        "next_open": ck.get("next_open"), "next_close": ck.get("next_close"),
        "drop_pct": DROP_PCT, "universe": len(UNIVERSE),
        "max_positions": MAX_POSITIONS, "risk_pct": RISK_PCT,
        "signals": sigs, "queued": placed, "exits": exits,
        "open_positions": [{"symbol": p["symbol"], "qty": p["qty"],
                            "entry": round(float(p["avg_entry_price"]), 2),
                            "price": round(float(p["current_price"]), 2),
                            "pnl": round(float(p["unrealized_pl"]), 2),
                            "pct": round(float(p["unrealized_plpc"]) * 100, 2)} for p in pos],
        "open_orders": len(open_orders()),
    }
    with open("reversal_status.json", "w") as f:
        json.dump(snap, f, indent=2)
    publish(snap)
    log(f"done. {len(sigs)} signal(s), {len(placed)} queued, {len(exits)} exits, "
        f"{len(pos)} position(s) held.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
