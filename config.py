"""Trading bot settings. Edit these values to change what the bot trades."""

# What the paper account started with, for "total profit since the start".
STARTING_BALANCE = 50_000

# Stocks to trade. Every enabled strategy runs on every stock.
# Removing a stock stops new trades in it, but shares the bot already holds
# in that stock are left alone (sell them yourself in Alpaca if you want).
SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY"]

# Dollars each strategy puts into each stock (rounded down to whole shares).
# 4 stocks x 4 strategies x $3,000 = up to $48,000 invested at once.
TRADE_DOLLARS = 3_000

# Strategies to run on stocks. Remove any you want to switch off:
# "crossover", "rsi", "momentum", "breakout"
ENABLED_STRATEGIES = ["crossover", "rsi", "momentum", "breakout"]

# --- ORB experiment (orb_live.py) ---
# Day-trades the Opening Range Breakout on the 20 "stocks in play" each
# morning, separately from the strategies above. It never trades the stocks
# in SYMBOLS or anything else the account already holds, and closes every
# position 5 minutes before the market closes.
ORB_ENABLED = True
# Money the experiment sizes its trades from: 1% of this at risk per trade,
# and at most 4x this in total positions at once (intraday only).
ORB_CAPITAL = 25_000
# Stop-loss distance as a fraction of the stock's 14-day ATR, by start date:
# each entry applies from its date until the next one. The paper used 10%,
# but live trades at 10% were stopped out within seconds. In the backtest 30%
# and 50% lost far less, so each gets 2 market days live before we compare.
# Wider stops mean fewer shares for the same dollar risk.
ORB_STOP_ATR_FRACTIONS = {
    "2026-10-28": 0.10,
    "2026-11-04": 0.30,  # Wed Nov 4 and Thu Nov 5
    "2026-11-06": 0.50,  # Fri Nov 6 and Mon Nov 9, then decide
}
# Skip stocks opening at or under this price: their stops end up barely
# wider than the bid-ask spread.
ORB_MIN_PRICE = 10
# "iex" is the free plan's live data (one exchange, a stand-in for the whole
# market). Switch to "sip" only with Alpaca's Algo Trader Plus subscription.
ORB_FEED = "iex"

# --- Crypto ---
# Traded every day, weekends included. Use Alpaca's symbol format (BTC/USD).
CRYPTO_SYMBOLS = ["BTC/USD", "ETH/USD"]
CRYPTO_STRATEGIES = ["crossover", "breakout"]
# Dollars each strategy puts into each coin (fractional coins are fine).
# 2 coins x 2 strategies x $3,000 = up to $12,000 invested at once.
CRYPTO_TRADE_DOLLARS = 3_000

# How many trading days of history to download. Momentum needs at least
# MOMENTUM_LOOKBACK + 1 days, so keep this comfortably above 252.
LOOKBACK_DAYS = 300

# --- Moving average crossover ---
# Hold while the short average is above the long average.
SHORT_WINDOW = 20
LONG_WINDOW = 50

# --- RSI mean reversion ---
# Buy when RSI drops below RSI_BUY_BELOW (oversold), sell above RSI_SELL_ABOVE.
RSI_PERIOD = 14
RSI_BUY_BELOW = 30
RSI_SELL_ABOVE = 70

# --- Momentum ---
# At each month end, hold if the price is higher than MOMENTUM_LOOKBACK days ago.
MOMENTUM_LOOKBACK = 252  # about 12 months

# --- Breakout ---
# Buy on a new BREAKOUT_ENTRY-day high, sell on a new BREAKOUT_EXIT-day low.
BREAKOUT_ENTRY = 50
BREAKOUT_EXIT = 20
