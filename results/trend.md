# Trend-following (noise boundary) on US100 / NQ=F

Costs per side: 1.25 pts (spread 1.5 round trip + 0.5 slippage each side).

## 1h bars, ~2 years (session from 10:00): 585 sessions 2024-07-01 .. 2026-11-18

| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 328 | 39.6 | 102.3 | -76.8 | -5.84 | -1914.2 | -524.5 | -1389.8 | 42.4 | -812.5 | -3129.2 | -2138.3 | 55% |
| 1.0 | 258 | 42.2 | 96.9 | -82.7 | -6.8 | -1753.5 | -155.2 | -1598.2 | 44.3 | -812.5 | -2708.5 | -1300.2 | 40% |
| 1.5 | 162 | 46.3 | 91.1 | -79.7 | -0.62 | -101.2 | 14.2 | -115.5 | 47.1 | -373.2 | -879.2 | -1103.1 | 75% |

Always-long intraday (open->close every day): total -2541.0 pts, max DD -4593.5

band 0.75 by year: {'2024': 436.6, '2025': -909.5, '2026': -1441.0}
band 1.0 by year: {'2024': 282.9, '2025': -599.0, '2026': -1437.0}
band 1.5 by year: {'2024': 194.2, '2025': 201.6, '2026': -496.6}

## 30m bars, ~60 days: 49 sessions 2026-09-11 .. 2026-11-18

| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 30 | 46.7 | 160.2 | -105.7 | 18.36 | 550.8 | 470.0 | 80.8 | 47.8 | -277.8 | -441.5 | 419.7 | 60% |
| 1.0 | 35 | 40.0 | 148.5 | -94.0 | 3.01 | 105.2 | 491.8 | -386.5 | 40.9 | -361.0 | -813.8 | 306.9 | 35% |
| 1.5 | 19 | 42.1 | 163.7 | -83.2 | 20.75 | 394.2 | 323.2 | 71.0 | 50.0 | -248.0 | -487.8 | 90.2 | 70% |

Always-long intraday (open->close every day): total -2386.0 pts, max DD -2830.2

band 0.75 by year: {'2026': 550.8}
band 1.0 by year: {'2026': 105.4}
band 1.5 by year: {'2026': 394.1}

## 5m bars, ~60 days (checks every 30 min): 48 sessions 2026-09-11 .. 2026-11-18

| band x | trades | win% | avg win | avg loss | pts/trade | total pts | 1st half | 2nd half | green days% | worst day | max DD | random total | beats random |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0.75 | 43 | 30.2 | 202.6 | -49.9 | 26.47 | 1138.0 | 463.8 | 674.2 | 40.0 | -335.5 | -435.2 | 737.3 | 80% |
| 1.0 | 41 | 29.3 | 185.2 | -48.0 | 20.29 | 831.8 | 523.8 | 308.0 | 42.9 | -300.2 | -589.5 | 493.1 | 70% |
| 1.5 | 26 | 30.8 | 227.9 | -48.1 | 36.84 | 957.8 | 307.5 | 650.2 | 46.2 | -176.0 | -213.5 | 640.0 | 80% |

Always-long intraday (open->close every day): total -2112.5 pts, max DD -2556.8

band 0.75 by year: {'2026': 1138.0}
band 1.0 by year: {'2026': 831.8}
band 1.5 by year: {'2026': 957.8}
