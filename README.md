# NAS100 Scalper: MT5 bot + phone app

This setup has two parts:
- **A phone app (MT5 Control):** shows whether the bot is connected, your MT5 account, a live analysis chart, Start/Stop buttons, open positions and an activity log.
- **A bot:** scalps NAS100 (Exness `USTEC`) through the whole New York session and opens each trade as a stack of 10 positions.

Everything runs for free on GitHub. The bot reaches your MT5 account through MetaApi, so you don't need a laptop.

## How the bot trades
All times below are New York time.

| Time | What it does |
|---|---|
| 09:15 | Connects to MT5 and starts building 1-minute candles |
| 09:35 | **Opening-range trade:** follows the direction of the first 5-minute candle. Stop goes on the other side of that candle. Take-profits are spread from 2R to 10R |
| 09:40–14:45 | **Pullback scalps:** finds the trend with EMA 9 vs EMA 21 and waits for price to dip to EMA 9. Enters when a 1-minute candle closes back in the trend direction. Stop goes beyond the last 3 candles. Take-profits are spread from 1R to 2.5R |
| 15:00 | Closes everything and disconnects |

**Tunis time:** 15:15–21:00 in US winter time (Nov → mid-Mar), 14:15–20:00 in US summer time.

### Risk rules (built in)
- **Stacking:** each setup opens **10 positions together**, but they **share one risk budget**. All 10 together risk 0.5% of the balance, the same as one normal trade.
- **Breakeven:** once half the stack has taken profit, the rest move their stop to breakeven.
- **Limits:**
  - one setup at a time
  - at most 8 setups a day
  - 3-minute pause after each setup closes
- **Daily loss limit:** if the day reaches −2%, it closes everything and stops until the next day.
- **Real accounts blocked:** the bot won't trade a real account until you set `ALLOW_REAL_ACCOUNT=true`.

You can change these in **Settings → Secrets and variables → Actions → Variables**:
`RISK_PERCENT`, `STACK_SIZE`, `DAILY_LOSS_PERCENT`, `MAX_SETUPS`, `MAX_LOT`, `SYMBOL`.

---

## Aggressive mode (fast scalping)
Turn it on with the GitHub variable `STYLE` = `aggressive`. Remove the variable, or set it to `standard`, to go back.

**How it trades:**
- **Scanning:** checks every 1-minute candle from 09:35 to 14:45 New York (15:35–20:45 Tunis, winter time).
- **BUY:** the candle closes above EMA 9, EMA 9 is above VWAP and rising, and the candle is green. **SELL** is the mirror image.
- **Exits live on Exness's server:**
  - The take-profit sits at **+5 points of real profit after the spread**, so Exness closes the trade the instant price touches it, with no delay from the bot.
  - The **emergency stop** sits 50 points away. Small dips below the entry are ignored.
- **Repeats:** the bot checks every 5 seconds whether the trade has closed, then looks for the next entry on the next candle.
- **Skips:** no entry when the spread is wider than normal, or when the candle has already run far from EMA 9.

**Safety:**
- risk 0.2% per trade (≈ $2 at the emergency stop on $1,000, about 0.04 lot)
- max 60 trades a day
- a 15-minute pause after 2 emergency stops in a row
- the −2% daily limit

| Variable | Default | Meaning |
|---|---|---|
| `TARGET_POINTS` | 5 | profit target per trade, in points after the spread |
| `EMERGENCY_STOP_POINTS` | 50 | emergency stop distance |
| `RISK_PERCENT` | 0.2 | % of balance lost if the emergency stop is hit |
| `MAX_TRADES` | 60 | trades per day |

**The maths to watch:** a win is about +$0.20 and a stop is about −$2.00, so **one stop cancels about 10 wins**. The bot only makes money if it wins **more than about 91%** of trades. Check the ✓/✗ counter in the app every day of the demo test.

---

## Setup (all on your phone)

### 1. Exness demo account
1. In the Exness app, create an **MT5 Demo** account.
2. Write down the **login**, **password** and **server** (for example `Exness-MT5Trial9`).
3. In MT5, check the exact name of NAS100: `USTEC`, or `USTECm` on Standard accounts.

### 2. MetaApi
1. In your phone browser, sign up at **app.metaapi.cloud**.
2. **Add account**: choose MT5 and enter the login, password and server.
3. Copy the **account ID**.
4. Create an **API token** and copy it.

### 3. GitHub repository
1. The repository must be **public**. That's what makes GitHub's servers and the app hosting free. Your passwords stay hidden in Secrets.
   - Note: the bot's status file (balance, open trades) is visible to anyone who finds the repo. Your login number and passwords are never written to it.
2. Go to **Settings → Secrets and variables → Actions**.
   - **Secrets:** add `METAAPI_TOKEN` and `METAAPI_ACCOUNT_ID`.
   - **Variables:** add `SYMBOL` = `USTEC` (or `USTECm`).
3. Go to **Settings → Actions → General → Workflow permissions** and choose **Read and write**.
4. Go to **Settings → Pages**. Under "Deploy from a branch", choose **main** and **/docs**, then save. After about a minute, your app is live at `https://<your-username>.github.io/<repo-name>/`.

### 4. Token for the app
1. On GitHub, open **Settings → Developer settings → Fine-grained tokens → Generate new token**.
2. Under **Repository access**, choose **only this repository**.
3. Under **Permissions**, set **Contents: Read and write** and **Actions: Read and write**.
4. Open the app link, tap **⚙**, and paste the repository name and the token. The token is saved only on your phone.
5. Tip: in your browser menu, choose **Add to Home screen** so it opens like a real app.

### 5. Start
Tap **▶ START BOT**.
- **During the NY session:** it connects in 1–3 minutes and the screen shows **CONNECTED**.
- **Outside the session:** it shows **ARMED** and starts by itself every weekday at 09:15 New York.

**■ STOP BOT** (tap twice) closes all bot positions within 30 seconds and stops automatic starts.

---

## Costs
- **GitHub** (servers + app hosting): free for a public repository.
- **MetaApi:** the account is connected only during the session, about 6 hours each weekday. Check MetaApi's billing page after the first day.
  - If the free tier and $5 bonus don't cover it, set `MAX_SETUPS` lower, or start the bot only on the days you want.

## Before you use real money
- The "live analysis" in this app is **real**. It shows the actual EMAs and checks the bot is using, not an animation.
- Stacking 10 positions makes the screen look like the video. It does **not** improve results, because the risk is the same as one trade.
- This strategy hasn't been backtested on Exness data. A published replication of the opening-range part on Nasdaq CFDs found the edge was **about zero after spreads**. Scalping has the same problem: the spread is a big share of each small win.
- **Run on demo for at least 2 months.** Only consider going live if the results are positive and you can accept the losing days. Even then, use the minimum lot.
