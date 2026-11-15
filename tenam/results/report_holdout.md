# 10 AM model: holdout (2024-01-02 to 2026-11-13)

Costs: $0.01/share slippage on entry and exit, $0.00 commission. Target 4R, last entry 11:30. R is per trade, after slippage.

## Verdict: inconclusive. By the section 4 rules: not worth trading.

On the holdout, with settings frozen beforehand, the model made a small profit after costs: +0.079R a trade
on QQQ (747 trades) and +0.089R on SPY (730), profit factor 1.11 and 1.13. It held up at $0.02 slippage
(+0.054R and +0.067R), every QQQ year was positive, neighboring settings were mostly positive, and it beat
94% (QQQ) and 98.5% (SPY) of random-direction runs. That is better than chance would usually give.

It misses the bar: +0.15R a trade and a profit factor of 1.3. Over the development period, the same
settings made only +0.014R (QQQ) and +0.028R (SPY). Over all 2019-2026 QQQ trades that is about +0.04R a
trade, under one standard error from zero. A real but small edge and a lucky three years both fit these numbers.

The video window (the 90 trading days before 2026-07-31) was an unusually good stretch for QQQ: +0.236R a trade and
profit factor 1.32, three times the holdout average. SPY made +0.024R in the same days. A highlight
reel drawn from a window like that looks much better than the full record.

## Summary

| | QQQ | SPY |
|---|---|---|
| trading days | 721 | 721 |
| days skipped (range > 2x median) | 37 | 63 |
| trades | 747 | 730 |
| trades per day | 1.036 | 1.012 |
| days with a trade | 75.6% | 79.1% |
| win rate | 26.1% | 30.7% |
| breakeven win rate (at these avg win/loss) | 24.2% | 28.2% |
| avg win R | 3.174 | 2.562 |
| avg loss R | -1.015 | -1.006 |
| expectancy R | 0.079 | 0.089 |
| expectancy R before slippage | 0.103 | 0.110 |
| total R | 58.9 | 64.8 |
| profit factor | 1.105 | 1.127 |
| max drawdown R | -25.0 | -27.9 |
| longest losing streak | 16 | 13 |
| worst day | -4.1R (2026-03-20) | -4.1R (2025-12-02) |
| worst week | -8.2R (week ending 2024-09-20) | -10.1R (week ending 2025-05-23) |
| worst month | -10.6R (2026-01) | -18.0R (2024-09) |
| best month | +15.8R (2024-07) | +24.5R (2025-04) |
| best year | +31.2R (2024) | +53.4R (2025) |
| total R without best month | 43.1 | 40.2 |
| expectancy without best month | 0.059 | 0.057 |
| total R without best year | 27.7 | 11.4 |
| expectancy without best year | 0.057 | 0.024 |
| stop at the 0.05% minimum | 96.5% | 99.0% |
| median stop distance | $0.806 (0.150%) | $0.916 (0.150%) |
| exits at target / stop / time | 17% / 73% / 10% | 13% / 67% / 20% |

## QQQ (main)

By year

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| 2024 | 258 | 26.0% | +0.121 | +31.2 |
| 2025 | 253 | 26.5% | +0.027 | +6.9 |
| 2026 | 236 | 25.8% | +0.088 | +20.8 |

By weekday

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| Mon | 131 | 26.0% | +0.001 | +0.1 |
| Tue | 151 | 31.1% | +0.340 | +51.4 |
| Wed | 163 | 25.2% | -0.006 | -1.0 |
| Thu | 145 | 20.7% | -0.085 | -12.3 |
| Fri | 157 | 27.4% | +0.132 | +20.7 |

Long vs short

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| long | 412 | 27.4% | +0.089 | +36.5 |
| short | 335 | 24.5% | +0.067 | +22.4 |

By trade number of the day

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| 1 | 545 | 27.0% | +0.114 | +62.3 |
| 2 | 168 | 25.0% | +0.059 | +9.8 |
| 3 | 33 | 18.2% | -0.371 | -12.2 |
| 4 | 1 | 0.0% | -1.023 | -1.0 |

By exit

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| stop | 544 | 0.0% | -1.024 | -557.3 |
| target | 128 | 100.0% | +3.975 | +508.8 |
| time | 75 | 89.3% | +1.431 | +107.3 |

Dollars (1% risk per trade, compounding, no leverage)

|  | start $10,000 | start $50,000 |
|---|---|---|
| end | $10,784 | $54,014 |
| return | +7.8% | +8.0% |
| per year | +2.7% | +2.7% |
| max drawdown | $-394 (-3.7%) | $-2,000 (-3.8%) |
| trades where the no-leverage cap cut the size | 100% | 100% |
| average risk actually taken | 0.15% of equity | 0.15% of equity |

## SPY

By year

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| 2024 | 255 | 29.0% | +0.062 | +15.9 |
| 2025 | 246 | 33.7% | +0.217 | +53.4 |
| 2026 | 229 | 29.3% | -0.020 | -4.5 |

By weekday

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| Mon | 136 | 28.7% | -0.020 | -2.8 |
| Tue | 153 | 33.3% | +0.226 | +34.6 |
| Wed | 148 | 29.7% | +0.058 | +8.6 |
| Thu | 142 | 31.0% | +0.159 | +22.6 |
| Fri | 151 | 30.5% | +0.011 | +1.6 |

Long vs short

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| long | 411 | 32.1% | +0.088 | +36.3 |
| short | 319 | 28.8% | +0.089 | +28.4 |

By trade number of the day

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| 1 | 570 | 31.6% | +0.106 | +60.3 |
| 2 | 142 | 27.5% | +0.002 | +0.3 |
| 3 | 17 | 29.4% | +0.307 | +5.2 |
| 4 | 1 | 0.0% | -1.020 | -1.0 |

By exit

|  | trades | win rate | avg R | total R |
|---|---|---|---|---|
| stop | 491 | 0.0% | -1.028 | -504.6 |
| target | 94 | 100.0% | +3.978 | +373.9 |
| time | 145 | 89.7% | +1.348 | +195.5 |

Dollars (1% risk per trade, compounding, no leverage)

|  | start $10,000 | start $50,000 |
|---|---|---|
| end | $10,975 | $54,927 |
| return | +9.7% | +9.9% |
| per year | +3.3% | +3.3% |
| max drawdown | $-429 (-4.0%) | $-2,210 (-4.1%) |
| trades where the no-leverage cap cut the size | 100% | 100% |
| average risk actually taken | 0.15% of equity | 0.15% of equity |

## Frozen settings

Ran 2026-11-15T15:11:02. Minimum stop 0.15% of price (change 1), target 4R, last entry 11:30, slippage $0.01/share each way.

File hashes (sha256, first 16): `config.py` 27ce47554b73cb5c, `strategy.py` 75996aab5b983325, `backtest.py` 83faf4cc2a7cee98, `report.py` 2529a60bcd82b940, `propfirm_sim.py` ac143457097dd509, `phase3.py` 807656961a46b995

## Worth continuing? (your section 4 criteria, on this period)

| criterion | QQQ | SPY |
|---|---|---|
| expectancy >= +0.15R with >= 200 trades | +0.079R on 747 trades | +0.089R on 730 trades |
| profit factor >= 1.3 | 1.11 | 1.13 |
| positive years | 3 of 3 | 2 of 3 |
| positive quarters | 8 of 12 | 8 of 12 |
| beats >= 95% of random-direction runs | 94% | 98% |
| neighboring settings positive | 5 of 6 | 6 of 6 |
| max drawdown | -25.0R ($12,487 at $500 a trade) | -27.9R ($13,970 at $500 a trade) |

## Development vs this period (same frozen settings)

QQQ

| | development | this period | this period at $0.02 slippage |
|---|---|---|---|
| trades | 1276 | 747 | 747 |
| win rate | 23.1% | 26.1% | 26.1% |
| expectancy R | +0.014 | +0.079 | +0.054 |
| total R | 17.7 | 58.9 | 40.7 |
| profit factor | 1.02 | 1.11 | 1.07 |
| max drawdown R | -68.0 | -25.0 | -26.9 |

SPY

| | development | this period | this period at $0.02 slippage |
|---|---|---|---|
| trades | 1232 | 730 | 730 |
| win rate | 26.2% | 30.7% | 30.4% |
| expectancy R | +0.028 | +0.089 | +0.067 |
| total R | 34.6 | 64.8 | 49.0 |
| profit factor | 1.04 | 1.13 | 1.09 |
| max drawdown R | -39.5 | -27.9 | -28.8 |

## Random-direction baseline

Every trade's entry time, entry price, stop distance and 4R target kept; direction flipped at random, 200 runs, same exit rules and slippage. Overlaps that a one-position rule would have prevented are not removed.

| | QQQ | SPY |
|---|---|---|
| strategy | +0.079 | +0.089 |
| random mean | -0.010 | -0.016 |
| random 5th pct | -0.101 | -0.103 |
| random 95th pct | +0.085 | +0.067 |
| strategy beats | 94.0% | 98.5% |

## Parameter sensitivity

Expectancy per trade after slippage (trades, profit factor). The frozen setting is in bold.

QQQ, development

| target \ last entry | 10:30 | 11:30 | 12:30 |
|---|---|---|---|
| 2R | -0.088R (604, PF 0.88) | -0.035R (1344, PF 0.95) | -0.024R (1828, PF 0.97) |
| 3R | -0.045R (597, PF 0.94) | -0.010R (1297, PF 0.99) | -0.023R (1729, PF 0.97) |
| 4R | -0.036R (595, PF 0.96) | **+0.014R** (1276, PF 1.02) | -0.012R (1692, PF 0.98) |
| 5R | +0.033R (595, PF 1.04) | +0.053R (1273, PF 1.06) | +0.032R (1684, PF 1.04) |

| minimum stop | expectancy | trades | profit factor |
|---|---|---|---|
| 0.10% | -0.061R | 1448 | 0.93 |
| **0.15%** | +0.014R | 1276 | 1.02 |
| 0.20% | +0.037R | 1172 | 1.05 |

QQQ, this period

| target \ last entry | 10:30 | 11:30 | 12:30 |
|---|---|---|---|
| 2R | +0.021R (364, PF 1.03) | +0.040R (789, PF 1.06) | -0.014R (1053, PF 0.98) |
| 3R | +0.027R (362, PF 1.04) | +0.092R (760, PF 1.13) | +0.051R (1005, PF 1.07) |
| 4R | -0.002R (362, PF 1.00) | **+0.079R** (747, PF 1.11) | +0.050R (978, PF 1.07) |
| 5R | -0.007R (362, PF 0.99) | +0.063R (743, PF 1.08) | +0.034R (968, PF 1.04) |

| minimum stop | expectancy | trades | profit factor |
|---|---|---|---|
| 0.10% | +0.055R | 861 | 1.07 |
| **0.15%** | +0.079R | 747 | 1.11 |
| 0.20% | +0.092R | 680 | 1.13 |

SPY, development

| target \ last entry | 10:30 | 11:30 | 12:30 |
|---|---|---|---|
| 2R | +0.051R (621, PF 1.08) | -0.004R (1279, PF 0.99) | -0.044R (1667, PF 0.94) |
| 3R | +0.060R (618, PF 1.08) | +0.007R (1240, PF 1.01) | -0.047R (1593, PF 0.94) |
| 4R | +0.070R (618, PF 1.10) | **+0.028R** (1232, PF 1.04) | -0.033R (1571, PF 0.96) |
| 5R | +0.082R (618, PF 1.11) | +0.069R (1232, PF 1.09) | +0.006R (1569, PF 1.01) |

| minimum stop | expectancy | trades | profit factor |
|---|---|---|---|
| 0.10% | -0.041R | 1413 | 0.95 |
| **0.15%** | +0.028R | 1232 | 1.04 |
| 0.20% | +0.100R | 1137 | 1.15 |

SPY, this period

| target \ last entry | 10:30 | 11:30 | 12:30 |
|---|---|---|---|
| 2R | +0.046R (374, PF 1.07) | +0.037R (745, PF 1.06) | +0.026R (940, PF 1.04) |
| 3R | +0.121R (372, PF 1.18) | +0.072R (735, PF 1.11) | +0.062R (910, PF 1.09) |
| 4R | +0.130R (372, PF 1.19) | **+0.089R** (730, PF 1.13) | +0.065R (901, PF 1.09) |
| 5R | +0.148R (372, PF 1.21) | +0.077R (730, PF 1.11) | +0.063R (898, PF 1.09) |

| minimum stop | expectancy | trades | profit factor |
|---|---|---|---|
| 0.10% | +0.083R | 840 | 1.11 |
| **0.15%** | +0.089R | 730 | 1.13 |
| 0.20% | +0.082R | 679 | 1.13 |

## Prop-firm simulation (placeholder rules)

$50k account, pass at +$3,000, fail at $2,000 below the high-water mark (checked after each trade) or a $1,000 losing day, $500 (1%) at risk per trade so each R is $500. A new attempt starts every trading day in the period.

| | QQQ | SPY |
|---|---|---|
| attempts | 721 | 721 |
| pass rate | 19.0% | 22.7% |
| fail rate | 78.2% | 76.8% |
| unfinished (data ran out) | 2.8% | 0.4% |
| of finished, passed | 19.5% | 22.8% |
| failed on daily loss | 62.1% | 61.2% |
| failed on trailing drawdown | 16.1% | 15.7% |
| avg trading days to pass | 5.0 | 6.8 |
| avg trading days to fail | 3.9 | 4.5 |

## Video window

The 90 trading days before 2026-07-31 (2026-03-23 to 2026-07-30), next to the whole period.

QQQ

| | video window | whole period |
|---|---|---|
| trades | 102 | 747 |
| win rate | 26.5% | 26.1% |
| expectancy R | +0.236 | +0.079 |
| total R | 24.1 | 58.9 |
| profit factor | 1.32 | 1.11 |
| max drawdown R | -8.234 | -25.0 |

SPY

| | video window | whole period |
|---|---|---|
| trades | 89 | 730 |
| win rate | 29.2% | 30.7% |
| expectancy R | +0.024 | +0.089 |
| total R | 2.2 | 64.8 |
| profit factor | 1.03 | 1.13 |
| max drawdown R | -15.0 | -27.9 |

## Caveats

- QQQ and SPY stand in for NQ and ES futures, which Alpaca doesn't offer. The video most likely trades
  futures, with different hours, tick sizes and leverage.
- A 1-minute backtest can't see inside a bar. When a bar touches both the stop and the target, it counts as a loss.
- Paper fills are kinder than real fills, especially on fast moves.
- Data: Alpaca SIP 1-minute bars, regular hours, New York time.
- This is research, not financial advice.
