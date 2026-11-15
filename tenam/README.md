# 10 AM model

A test of a mechanical "10 AM" break-and-retest day-trading model on QQQ and SPY: a backtest on development
data, a single run on held-out data, and a paper-only forward test. The rules are in [strategy.py](strategy.py);
every number is in [config.py](config.py).

**The rules in brief:**
- **Opening range:** the high and low from 9:30 to 9:59 ET.
- **Break:** a 5-minute bar after 10:00 closes outside the range.
- **Retest:** a later 1-minute bar comes back to the range edge and closes outside it again.
- **Entry:** the next bar's open, no later than 11:30.
- **Stop:** beyond the retest bar, at least 0.15% of the price away.
- **Target:** 4 times the stop distance.
- **Exit:** any trade still open is closed at 3:55.

## Results so far

| Period | QQQ | SPY |
|---|---|---|
| Development, 2019-2023, original 0.05% stop | -0.139R a trade (1,699 trades) | -0.101R (1,769) |
| Development, 0.15% stop (change 1) | +0.014R (1,276) | +0.028R (1,232) |
| Holdout, 2024 to Nov 2026, run once with frozen settings | +0.079R (747), profit factor 1.11 | +0.089R (730), profit factor 1.13 |

**Verdict: inconclusive.**
- **For an edge:** the holdout beat 94% (QQQ) and 98.5% (SPY) of random-direction runs.
- **Against:** it falls short of the bar set beforehand (+0.15R a trade, profit factor 1.3).
- **Over 2019 to 2026:** QQQ averages +0.04R a trade, under one standard error from zero.

Reports: [results/report_dev.md](results/report_dev.md), [results/change1_dev.md](results/change1_dev.md),
[results/report_holdout.md](results/report_holdout.md).

## Running it

```
python data.py                      # download and cache 1-minute SIP bars (once; later runs add new days)
python -m pytest -q                 # strategy and live-bot tests
python chart_day.py 2021-08-19      # draw one day: range, break, retest, entry, stop, target
python backtest.py dev && python report.py dev
python phase3.py --check            # rehearse the holdout run on development data
python phase3.py                    # the holdout run: refuses to run a second time
```

## Paper forward test (Phase 4)

[live_paper.py](live_paper.py) trades the same strategy code on the paper account. Live 1-minute IEX bars come
from Alpaca's stream, and the REST API fills any gaps. Each signal becomes a market bracket order with the stop and
the 4R target attached.

**Guards:**
- It runs only on the paper API, only on account `PAPER_ACCOUNT_ID`, and only with equity of at least $25k.
- At most 5 trades a day, one position at a time, and no new trades once the day reaches -3R.
- It skips early-close days and closes its own trades at 3:55 ET.
- A file named `KILL` in this folder closes its trades and stops it.

**Shared account:** the account also runs the other bots. This one touches only its own orders (client ids
starting `tenam-`) and the shares it bought. It skips a signal if the account already holds QQQ. A trade risks
about $37 (36 shares at most $25k), because R is what's being measured.

```
python live_paper.py --replay 2026-11-13   # a past day through the bot with a fake broker (no orders)
python live_paper.py --dry-run             # today, live data, orders logged but not sent
python weekly_report.py                    # live vs backtest: signal match, fill slippage, R difference
touch KILL                                 # stop it today (delete the file to resume)
```

**Schedules:** launchd starts it at 8:25 AM Central (9:25 ET) Monday to Friday
(`launchd/com.presleybarr.tradingbot.tenam.plist`). The weekly report runs Fridays at 4:30 PM Central
(`...tenam-weekly.plist`).

**Logs:**
- `logs/live.log`: everything.
- `logs/trades_live.csv`: one row per signal, with the intended entry, stop and target, the fills, R, and slippage.
- `logs/days_live.csv`: one row per day.

**How long:** run it at least 60 trading days before drawing conclusions. Even then, about 60-75 trades can only
tell apart edges larger than about ±0.25R a trade.

## Caveats
- QQQ and SPY stand in for NQ and ES futures, which Alpaca doesn't offer. The futures have different hours,
  tick sizes and leverage.
- A 1-minute backtest can't see inside a bar. When one bar touches both the stop and the target, it counts as a loss.
- Paper fills are kinder than real fills, especially on fast moves. The live bot sees IEX bars (one exchange),
  while the backtest uses SIP bars (the whole market).
- This is research, not financial advice.
