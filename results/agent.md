# Prediction agent - walk-forward test on real US100 data
408 baskets over 19 days. Train: 206 baskets (days < 2026-11-10). Test (never seen): 202 baskets.

| threshold | test: all baskets (n / win% / $) | test: agent-approved (n / win% / $) |
|---|---|---|
| 0.3 | (202, 86.6, 1514.98) | (191, 86.4, 1418.02) |
| 0.4 | (202, 86.6, 1514.98) | (167, 86.2, 1384.36) |
| 0.5 | (202, 86.6, 1514.98) | (130, 86.2, 1060.21) |
| 0.6 (chosen on training days) | (202, 86.6, 1514.98) | (85, 84.7, 703.93) |
| 0.7 | (202, 86.6, 1514.98) | (54, 87.0, 653.95) |
| 0.8 | (202, 86.6, 1514.98) | (20, 90.0, 501.55) |
| 0.9 | (202, 86.6, 1514.98) | (9, 88.9, 260.7) |

Weights (positive = more likely to win): atr_pct +0.80, burst +0.60, ema_slope -0.58, er +0.49, streak +0.42, min_since_open +0.28, count -0.28, spread_atr +0.26, vwap_dist -0.12, move10s -0.10, mtf_against -0.03, mtf_agree +0.03

Agent NOT enabled: on unseen days it turned +1514.98 into +703.93 at threshold 0.6