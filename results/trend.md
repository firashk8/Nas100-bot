# Trend-following (noise boundary) on US100 / NQ=F

Costs per side: 1.25 pts (spread 1.5 round trip + 0.5 slippage each side).

## 1h bars, ~2 years (session from 10:00): 589 sessions 2024-06-28 .. 2026-11-18

| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 366 | 39.3 | 100.5 | -74.8 | -5.85 | -2142.8 | -426.5 | -1716.2 | 43.2 | -929.8 | -3743.8 | -2468.7 | 55% |
| 1.0 | 280 | 41.1 | 96.9 | -80.9 | -7.89 | -2210.5 | -346.5 | -1864.0 | 44.6 | -809.2 | -3314.8 | -1315.7 | 25% |
| 1.5 | 174 | 46.0 | 90.9 | -75.9 | 0.79 | 137.8 | 47.2 | 90.5 | 47.4 | -426.8 | -1135.2 | -1179.3 | 95% |

Always-long intraday (open->close every day): total -2551.0 pts, max DD -4622.0

band 0.75 by year: {'2024': 562.2, '2025': -1128.0, '2026': -1577.0}
band 1.0 by year: {'2024': 259.9, '2025': -902.9, '2026': -1567.2}
band 1.5 by year: {'2024': 184.2, '2025': 74.8, '2026': -121.0}

## 30m bars, ~60 days: 50 sessions 2026-09-10 .. 2026-11-18

| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 42 | 40.5 | 136.1 | -88.4 | 2.45 | 103.0 | 401.5 | -298.5 | 45.8 | -528.0 | -605.2 | -17.4 | 55% |
| 1.0 | 43 | 39.5 | 125.7 | -89.0 | -4.1 | -176.2 | 507.5 | -683.8 | 43.5 | -446.2 | -918.2 | 122.1 | 35% |
| 1.5 | 19 | 42.1 | 165.9 | -83.3 | 21.63 | 411.0 | 339.2 | 71.8 | 50.0 | -248.2 | -489.0 | 113.7 | 70% |

Always-long intraday (open->close every day): total -2388.2 pts, max DD -2825.2

band 0.75 by year: {'2026': 103.1}
band 1.0 by year: {'2026': -176.1}
band 1.5 by year: {'2026': 411.1}

## 5m bars, ~60 days (checks every 30 min): 49 sessions 2026-09-10 .. 2026-11-18

| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 61 | 31.1 | 144.8 | -42.6 | 15.8 | 964.0 | 530.8 | 433.2 | 38.5 | -335.5 | -504.2 | 302.6 | 95% |
| 1.0 | 53 | 26.4 | 161.2 | -41.0 | 12.42 | 658.2 | 454.2 | 204.0 | 40.9 | -300.2 | -655.8 | 564.7 | 50% |
| 1.5 | 27 | 29.6 | 227.5 | -47.3 | 34.11 | 921.0 | 269.8 | 651.2 | 42.9 | -185.0 | -219.8 | 631.6 | 75% |

Always-long intraday (open->close every day): total -2114.8 pts, max DD -2551.8

band 0.75 by year: {'2026': 964.0}
band 1.0 by year: {'2026': 658.0}
band 1.5 by year: {'2026': 921.2}
