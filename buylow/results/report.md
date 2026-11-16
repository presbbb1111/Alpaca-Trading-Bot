# Buy low, sell high: backtest

$5,000 split into three sleeves of $1,666.67. Each is compared with simply holding $1,667 of VOO over the same dates. Costs are included: 0.03% a side on market orders; the ladder uses limit orders, filled only when the price trades through by a cent.

## 2019-2023

| | trades | win rate | avg per trade | profit/loss | return | a year | worst drop | days invested |
|---|---|---|---|---|---|---|---|---|
| 1. Dip buying (daily) | 602 | 65% | +0.23% | $+810 | +48.6% | +8.3% | -29.2% | 79% |
| 2. Price ladder on VOO | 192 | 100% | +1.08% | $+690 | +41.4% | +7.2% | -27.6% | 79% |
| 3. Intraday stretch below VWAP | 3755 | 49% | -0.06% | $-1,494 | -89.6% | -36.5% | -90.4% |  |
| Hold VOO | - | n/a | n/a | $+1,552 | +93.1% | +14.1% | -34.2% | 100% |
| All three together ($5,000) | 4549 | n/a | n/a | $+6 | +0.1% | +0.0% | -28.2% |  |

## 2024 to now

| | trades | win rate | avg per trade | profit/loss | return | a year | worst drop | days invested |
|---|---|---|---|---|---|---|---|---|
| 1. Dip buying (daily) | 440 | 64% | +0.34% | $+1,002 | +60.1% | +17.9% | -22.3% | 90% |
| 2. Price ladder on VOO | 159 | 100% | +1.07% | $+556 | +33.4% | +10.6% | -14.2% | 72% |
| 3. Intraday stretch below VWAP | 2199 | 48% | -0.08% | $-1,374 | -82.5% | -45.6% | -82.7% |  |
| Hold VOO | - | n/a | n/a | $+997 | +59.8% | +17.8% | -19.0% | 100% |
| All three together ($5,000) | 2798 | n/a | n/a | $+184 | +3.7% | +1.3% | -19.0% |  |

## Profit/loss by year

| year | 1. Dip buying (daily) | 2. Price ladder on VOO | 3. Intraday stretch below VWAP | Hold VOO ($1,667 each Jan) |
|---|---|---|---|---|
| 2019 | $+121 | $+208 | $-765 | $+513 |
| 2020 | $+55 | $+178 | $-258 | $+260 |
| 2021 | $+528 | $+273 | $-207 | $+442 |
| 2022 | $-303 | $+13 | $-196 | $-329 |
| 2023 | $+409 | $+17 | $-68 | $+395 |
| 2024 | $+679 | $+178 | $-893 | $+403 |
| 2025 | $-19 | $+202 | $-330 | $+262 |
| 2026 | $+340 | $+188 | $-152 | $+166 |

Ladder: realized profit from slices sold; slices still held are in the equity figures above.

## Stress tests

| | 2019-2023 | 2024 to now |
|---|---|---|
| Dip buying, costs doubled | +31.8% | +46.6% |
| Stretch, costs doubled | -98.9% | -95.3% |
| Ladder with 0.5% steps (vs 1%) | +49.9% | +45.2% |
| Ladder with 2.0% steps (vs 1%) | +36.6% | +22.1% |

Ladder at the end of 2019-2023: 1 slices still held, bought at $436.42.
Ladder at the end of 2024 to now: 4 slices still held, bought at $710.15, $702.97, $695.80, $688.63.

Stretch exits: stop 41%, target 39%, time 20%. Most traded: XOM, AVGO, PG, UNH, ABBV.
Dip trades hold 3 days (median), 10 at most. Most traded: AMGN, WMT, NFLX, LLY, AVGO.

## Caveats
- The stock list is today's largest companies, which flatters anything that buys dips in them (they all recovered).
- 5-minute and 1-minute bars can't show the order of prices inside a bar. When a bar touches both the stop and the target, it counts as a loss.
- Paper fills are kinder than real ones. Research, not financial advice.
