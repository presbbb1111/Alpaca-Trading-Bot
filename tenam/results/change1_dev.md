# Change 1 on development data: a wider minimum stop

Development period only (2019-01-02 to 2023-12-29). Everything else as in Phase 2: $0.01/share slippage
each way, 4R target, last entry 11:30. Proposed beforehand: 0.15% of price, or 25% of the opening range.
The rows around each show whether a result holds up at neighboring settings.

| minimum stop | symbol | trades | win rate | breakeven | expectancy R | before slippage | total R | profit factor | max DD R | target / stop / time |
|---|---|---|---|---|---|---|---|---|---|---|
| 0.05% (Phase 2) | QQQ | 1,699 | 19.7% | 22.5% | -0.139 | -0.014 | -236 | 0.85 | -240 | 20 / 80 / 0% |
| 0.05% (Phase 2) | SPY | 1,769 | 20.2% | 22.3% | -0.101 | -0.001 | -178 | 0.89 | -211 | 20 / 80 / 1% |
| 0.10% of price | QQQ | 1,448 | 20.6% | 21.9% | -0.061 | +0.010 | -88 | 0.93 | -159 | 19 / 79 / 1% |
| 0.10% of price | SPY | 1,413 | 22.2% | 23.1% | -0.041 | +0.012 | -58 | 0.95 | -97 | 18 / 77 / 5% |
| **0.15% of price** | QQQ | 1,276 | 23.1% | 22.8% | **+0.014** | +0.062 | +18 | 1.02 | -68 | 19 / 76 / 5% |
| **0.15% of price** | SPY | 1,232 | 26.2% | 25.5% | **+0.028** | +0.064 | +35 | 1.04 | -39 | 16 / 72 / 12% |
| 0.20% of price | QQQ | 1,172 | 25.6% | 24.7% | +0.037 | +0.074 | +44 | 1.05 | -43 | 16 / 73 / 11% |
| 0.20% of price | SPY | 1,137 | 31.7% | 28.8% | +0.100 | +0.128 | +114 | 1.15 | -35 | 13 / 65 / 22% |
| 15% of range | QQQ | 1,505 | 20.1% | 21.9% | -0.089 | -0.001 | -134 | 0.90 | -141 | 20 / 80 / 0% |
| 15% of range | SPY | 1,670 | 19.2% | 22.0% | -0.138 | -0.052 | -231 | 0.84 | -251 | 19 / 81 / 1% |
| **25% of range** | QQQ | 1,292 | 22.5% | 22.2% | **+0.014** | +0.072 | +18 | 1.02 | -58 | 20 / 77 / 3% |
| **25% of range** | SPY | 1,462 | 21.7% | 22.4% | **-0.035** | +0.031 | -51 | 0.96 | -92 | 19 / 78 / 3% |
| 35% of range | QQQ | 1,161 | 25.2% | 24.8% | +0.014 | +0.055 | +16 | 1.02 | -52 | 15 / 74 / 11% |
| 35% of range | SPY | 1,305 | 24.2% | 23.7% | +0.024 | +0.073 | +31 | 1.03 | -56 | 18 / 75 / 7% |

Is it more than noise? Standard error of the expectancy is about 0.055R per trade, so:

| minimum stop | symbol | expectancy | t | R by year (2019-2023) |
|---|---|---|---|---|
| 0.15% of price | QQQ | +0.014 | 0.25 | +23, -4, +21, +1, -23 |
| 0.15% of price | SPY | +0.028 | 0.52 | -7, +18, +10, -18, +32 |
| 0.20% of price | QQQ | +0.037 | 0.67 | +12, +33, +21, -10, -13 |
| 0.20% of price | SPY | +0.100 | 1.87 | -14, +21, +20, +38, +48 |
| 25% of range | QQQ | +0.014 | 0.25 | +18, +5, +12, +25, -41 |
| 25% of range | SPY | -0.035 | -0.67 | -45, +22, -33, -5, +10 |

None reaches t = 2, and the best cell (0.20%, SPY) is the best of 12 tries, so its t of 1.87 overstates it.
With the wider stops, 95-99% of trades use the minimum, so the stop is in effect a fixed distance
and the retest bar no longer sets it.

Full Phase 2-style report for 0.15% of price: report_dev_pct0015.md, equity_dev_pct0015.png.
