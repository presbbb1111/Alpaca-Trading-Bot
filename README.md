# Trading Bot

A bot that trades my Alpaca paper account. It runs a few simple strategies on stocks and crypto, tries a day-trading experiment, and sends everything to Discord.

Paper money only. Not financial advice.

## What runs and when

All times are Eastern.

- **9:30 AM daily:** `bot.py` checks each strategy and buys or sells if a signal changed. Stocks only trade on market days. Crypto trades every day.
- **9:15 AM to 3:55 PM, market days:** `orb_live.py` runs the opening range breakout experiment. It's closed out before 4 PM.
- **4:15 PM daily:** `summary.py` posts the day's results to Discord.
- **Always:** `dashboard.py` serves a dashboard at http://localhost:8050.

## Strategies

Each strategy gets $3,000 per symbol and keeps track of its own shares.

- **Crossover:** hold when the 20-day average is above the 50-day. Runs on stocks and crypto.
- **RSI:** buy when RSI drops below 30, sell when it goes above 70. Stocks only.
- **Momentum:** once a month, hold if the price is higher than a year ago. Stocks only.
- **Breakout:** buy at a new 50-day high, sell at a new 20-day low. Stocks and crypto.

The stocks are AAPL, MSFT, NVDA and SPY. The coins are BTC and ETH.

**ORB experiment:** at 9:35 it picks the 20 stocks with the most unusual opening volume. It then trades whichever way they break out of the first 5 minutes, with a tight stop. Nothing is held overnight. I'm running it to see what real fills look like, because the backtest couldn't tell me.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Put your Alpaca paper keys and Discord webhook URL in `.env`.

Check it works:

```bash
.venv/bin/python bot.py --dry-run
.venv/bin/python bot.py --test-discord
```

To run it on a schedule on a Mac:

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
for f in ~/Library/LaunchAgents/com.presleybarr.tradingbot*.plist; do launchctl bootstrap gui/$(id -u) "$f"; done
```

The plist files use this folder's full path and assume the Mac is on Central time, so edit them if either is different for you. The Mac also needs to be awake when the jobs run.

## Other commands

```bash
.venv/bin/python summary.py --print       # preview the daily summary
.venv/bin/python backtest.py              # backtest the strategies vs buy and hold
.venv/bin/python orb_backtest.py          # backtest ORB on QQQ
.venv/bin/python orb_stocks_in_play.py    # backtest ORB on stocks in play (big download)
```

## Settings

Everything is in `config.py`: symbols, trade sizes, which strategies run, and the ORB settings. If you remove a symbol or strategy that's holding shares, sell them first. Otherwise `positions.json` won't match the account.

## Backtest results

The full results are in `results/`.

- Buy and hold beat every strategy on all four stocks. The strategies did lose less in crashes.
- Crypto results depended mostly on the start date. RSI lost money every time, so crypto doesn't use it.
- ORB on QQQ made 15.9% a year after costs. Just holding QQQ made 19.1%.
- ORB on stocks in play ranged from big losses to big gains, depending on fills that historical data can't show. That's why it's running live on paper.
