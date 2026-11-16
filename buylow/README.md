# Buy low, sell high

Three ways to buy low and sell high, backtested with $1,667 each, and the two that held up paper-traded with $2,500
each. Every number is in [config.py](config.py); the rules are in [models.py](models.py), shared by the backtest
and the live bot.

**The rules:**
- **Dip buying (daily):** after the close, find stocks and ETFs above their 200-day average whose 2-day RSI is
  under 10 (a sharp 1-3 day drop). Buy the most oversold at the next open, up to 3 at a time, a third of the
  sleeve each. Sell at the open after a close above the 5-day average, or after 10 days.
- **Price ladder on VOO:** five limit buys, 1% apart under the anchor (the highest price since the ladder was
  last empty), a fifth of the sleeve each. Each slice that fills is sold 1% above its own buy. No stop-loss: in
  a long slide it ends up holding all five slices until the price recovers.
- **Intraday stretch (turned down):** buy a stock that closes a 5-minute bar more than 2 standard deviations
  below its VWAP, and sell back at the VWAP or at an equal-sized stop.

## Backtest (with costs)

| | 2019-2023 | 2024 to Nov 2026 | Biggest drop since 2024 |
|---|---|---|---|
| Dip buying | +8.3% a year (602 trades) | +17.9% a year (440) | -22% |
| Price ladder | +7.2% a year (192) | +10.6% a year (159) | -14% |
| Intraday stretch | -36.5% a year (3,755) | -45.6% a year (2,199) | -83% |
| Holding VOO | +14.1% a year | +17.8% a year | -19% |

None of them beat holding VOO. Dip buying matched it since 2024; the ladder made less with smaller drops. The
intraday stretch is about break-even before costs, and costs on three trades a day wore it down every year.
Full report: [results/report.md](results/report.md), chart: [results/equity.png](results/equity.png).

## Paper trading

Approved on Nov 16, 2026: dip buying and the ladder, $2,500 each ([status.json](status.json)).

```
python data.py                 # download the bars (once; later runs add new days)
python backtest.py             # the report above
python -m pytest -q            # the rules and the live bot against a fake broker
python live.py --dry-run       # today with live prices; orders only logged
python live.py --status        # what each sleeve holds
touch KILL                     # cancel its orders and stop (holdings stay); delete it to resume
```

**How it runs:** launchd starts `live.py` at 8:25 AM Central (9:25 ET), Monday to Friday
(`launchd/com.presleybarr.tradingbot.buylow.plist`). At the open it runs dip buying with market orders, then
keeps the ladder's limit orders working, checking every 30 seconds, until the close. Orders are day orders, so
it places them again each morning. After the close it writes each sleeve's value to `logs/days.csv`, which the
dashboard's New strategies card counts toward the 60-day paper test.

**Guards:**
- Only the paper API and account `PAPER_ACCOUNT_ID`, with equity of at least $25,000.
- Only its own orders (client ids starting `bl-`). It skips symbols the other bots hold or have orders in, and
  never trades the main bot's AAPL, MSFT, NVDA or SPY. The ladder trades VOO, not SPY, for the same reason.
- The approvals bot's morning clean-up leaves this bot's holdings alone.
- Its state is saved after every order (`state.json`), so a restart looks orders up instead of sending them again.

**Logs:** `logs/live.log` (everything), `logs/journal.txt` (fills and daily values), `logs/days.csv`.

## Caveats
- The stock list is today's largest companies, which flatters dip buying (they all recovered).
- 1- and 5-minute bars can't show the order of prices inside a bar; ties count against the strategy.
- Paper fills are kinder than real ones. This is research, not financial advice.
