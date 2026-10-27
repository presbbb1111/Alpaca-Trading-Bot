# Trading Bot

An automated trading bot for an [Alpaca](https://alpaca.markets) **paper trading** account. It runs four daily strategies on a list of stocks and two on crypto, day-trades an Opening Range Breakout experiment, and posts trades and daily summaries to Discord. It also includes backtesters for checking strategies against past prices.

> **Paper trading only.** This project is for learning and experimenting with simulated money. It is not financial advice, and past or backtested results don't predict future results.

## What it does

| Job | When (Eastern time) | What happens |
|---|---|---|
| **Daily bot** (`bot.py`) | Every day, 9:30 AM | Checks each strategy on each stock and coin and buys or sells when a signal changes. Stocks trade on market days only; crypto trades every day. |
| **ORB experiment** (`orb_live.py`) | Market days, 9:15 AM to 3:55 PM | Picks the 20 "stocks in play" at 9:35, trades breakouts of the first 5-minute range with tight stops, and closes everything before 4 PM. |
| **Daily summary** (`summary.py`) | Every day, 4:15 PM | Sends a Discord report: account value, profit/loss today and since the start, holdings, results by strategy, trades and bot health. |
| **Dashboard** (`dashboard.py`) | Always on | A live, view-only web page at [localhost:8050](http://localhost:8050) showing all of the above. |

Every trade also triggers a Discord alert, and every run is logged.

## Strategies

Each daily strategy answers one question every morning: *hold this, or stay in cash?* Each strategy has its own money on each symbol, and they never touch each other's shares.

| Strategy | Holds when | Used on |
|---|---|---|
| **Moving average crossover** (20/50) | The 20-day average price is above the 50-day average | Stocks, crypto |
| **RSI reversion** (30/70) | Bought when RSI drops below 30 (oversold), sold when it rises above 70 | Stocks |
| **Momentum** (12-month) | Checked monthly: the price is higher than 12 months ago | Stocks |
| **Breakout** (50/20) | Bought on a new 50-day high, sold on a new 20-day low | Stocks, crypto |

Signals use the last completed daily close and trade at the next morning's prices.

### ORB experiment

A day-trading strategy based on Zarattini, Barbon & Aziz (2024), run separately from the daily strategies:

1. At 9:35, rank liquid stocks (average volume ≥ 1M shares, ATR > $0.50, price > $5) by how unusual their volume was in the first 5 minutes, and take the top 20.
2. If the first 5-minute bar closed up, place a buy stop at its high; if it closed down, a short-sell stop at its low.
3. Once an entry fills, place a stop-loss 10% of the stock's ATR away.
4. Close everything at 3:55 PM. Nothing is held overnight.

It never trades the daily bot's stocks or anything else the account holds. It records how far each fill landed from its trigger price (slippage), since the backtest found the strategy's results depend heavily on fills that historical data can't show.

## Dashboard

Open **http://localhost:8050** in any browser on the Mac (or the *Trading Dashboard* app). It refreshes every 20 seconds and shows:

- **Account:** value, today's and all-time profit/loss, cash, and invested amount.
- **Account value chart:** 1 day, 1 week, 1 month or all time, with hover details.
- **By strategy:** open profit/loss for each strategy.
- **Signals:** each strategy on each symbol, holding or in cash, how close it is to changing, and anything that will trade on the next run.
- **Holdings and recent trades.**
- **ORB experiment:** today's 20 picks with live status (waiting, in trade, stopped out, closed), plus all-time slippage.
- **Bot health:** each scheduled job, its last run, and any errors today.

It can't place trades, and it only listens on the Mac itself (127.0.0.1), not the network. The design follows macOS's translucent "glass" look and switches between light and dark mode with the system setting.

## Project layout

```
bot.py                  Daily bot: runs the strategies, places orders, tracks shares per strategy
strategies.py           The four strategies (shared by the bot and the backtester)
config.py               All settings: symbols, trade sizes, strategy parameters, ORB settings
notify.py               Discord alerts
summary.py              End-of-day Discord summary
orb_live.py             Live ORB day-trading experiment
dashboard.py            Live dashboard server (http://localhost:8050)
web/index.html          The dashboard page

backtest.py             Backtests the daily strategies against buy-and-hold
orb_backtest.py         Backtests ORB on a single symbol (QQQ by default)
orb_stocks_in_play.py   Backtests ORB on stocks in play across ~6,600 US stocks

launchd/                macOS schedules for the three jobs and the dashboard
results/                Saved backtest results
requirements.txt        Python packages
.env.example            Template for your API keys and Discord webhook
```

These are created while the bot runs and are kept out of git:

```
.env                    Your Alpaca keys and Discord webhook (never commit this)
positions.json          Which shares each strategy owns
trades.txt              Daily bot trade journal, easy to read
orb_trades.txt          ORB trade journal
orb_fills.csv           Every ORB trade with fill prices and slippage
logs/                   Detailed logs (bot.log, orb.log, scheduler output)
data/                   Downloaded market data cache for the backtests
```

## Setup

**1. Install Python packages** (Python 3.11 or newer):

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**2. Add your keys.** Copy `.env.example` to `.env` and fill in:

- `ALPACA_API_KEY` and `ALPACA_SECRET_KEY`: from [app.alpaca.markets](https://app.alpaca.markets) → Paper Trading → API Keys.
- `DISCORD_WEBHOOK_URL`: in Discord, open a channel's settings → Integrations → Webhooks → New Webhook → Copy Webhook URL.

Keep `ALPACA_PAPER=true`.

**3. Check everything connects:**

```bash
.venv/bin/python bot.py --dry-run        # shows each strategy's signal, places no orders
.venv/bin/python bot.py --test-discord   # sends a test message to Discord
.venv/bin/python orb_live.py --dry-run   # shows the last trading day's ORB picks, no orders
.venv/bin/python summary.py --print      # shows the daily summary without sending it
```

**4. Schedule it (macOS).** The files in `launchd/` contain absolute paths to this folder. Edit them if you cloned it somewhere else. Times are in the Mac's local time; these assume Central time (8:30 AM Central = 9:30 AM Eastern).

```bash
cp launchd/*.plist ~/Library/LaunchAgents/
for f in ~/Library/LaunchAgents/com.presleybarr.tradingbot*.plist; do launchctl bootstrap gui/$(id -u) "$f"; done
```

To pause a job, run `launchctl bootout gui/$(id -u)/com.presleybarr.tradingbot` (or `.summary`, `.orb`, `.dashboard`).

The Mac must be awake at those times. To have it wake itself every day at 8:10 AM:

```bash
sudo pmset repeat wakeorpoweron MTWRFSU 08:10:00
```

## Running things by hand

```bash
.venv/bin/python bot.py                        # run the daily bot now
.venv/bin/python summary.py                    # send the daily summary now
.venv/bin/python backtest.py                   # backtest the strategies on config.SYMBOLS, 10 years
.venv/bin/python backtest.py --symbol BTC-USD --cost 0.0025
.venv/bin/python orb_backtest.py --symbol QQQ
.venv/bin/python orb_stocks_in_play.py         # first run downloads ~1.3 GB of data, about 2 hours (resumable)
```

## Settings

Everything is in `config.py`:

| Setting | Default | Meaning |
|---|---|---|
| `SYMBOLS` | AAPL, MSFT, NVDA, SPY | Stocks the daily strategies trade |
| `TRADE_DOLLARS` | $3,000 | Per strategy, per stock (whole shares) |
| `ENABLED_STRATEGIES` | all four | Strategies run on stocks |
| `CRYPTO_SYMBOLS` | BTC/USD, ETH/USD | Coins, in Alpaca's format |
| `CRYPTO_STRATEGIES` | crossover, breakout | Strategies run on crypto |
| `CRYPTO_TRADE_DOLLARS` | $3,000 | Per strategy, per coin (fractional) |
| `ORB_ENABLED` | True | Turns the ORB experiment on or off |
| `ORB_CAPITAL` | $25,000 | ORB risks 1% of this per trade, with at most 4× in positions at once |
| `ORB_FEED` | iex | Live data source; `sip` requires Alpaca's Algo Trader Plus plan |

Before changing `SYMBOLS` or removing a strategy while it holds shares, sell those shares first. Otherwise `positions.json` and the account fall out of sync.

## Backtest results so far

Saved in `results/`. In short:

- **Daily strategies:** over the last 5–10 years, simply holding beat every strategy on AAPL, MSFT, NVDA and SPY. The strategies' worst drops were usually smaller.
- **Crypto:** results swing heavily with the start date, and there's no consistent edge. RSI lost money in every crypto test, so crypto doesn't use it.
- **ORB on QQQ:** with a realistic 1¢ per share cost, it returned 15.9% a year against 19.1% for holding QQQ.
- **ORB on stocks in play:** the outcome depends on fills inside the first minute that historical bars can't show, ranging from big losses to big gains. At 2¢ per share, even the optimistic case only breaks even. That's why it's running as a live paper experiment.

## Known limits

- The Mac has to be on and awake, and your user account logged in, for the schedules to run.
- Paper fills are easier than real ones: orders fill at the quoted price, with no market impact.
- With the free data plan, the live ORB ranks relative volume from IEX alone, a small slice of the whole market, while the backtest used full-market data.
