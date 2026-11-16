"""Settings for the three "buy low, sell high" sleeves. Every number the
backtest and the live bot use is here.

$5,000 split evenly: $1,666.67 for each sleeve.
"""

from datetime import date

BUDGET = 5_000
SLEEVE = round(BUDGET / 3, 2)

# --- Data -------------------------------------------------------------------
DATA_START = date(2018, 1, 1)  # a year of warm-up for the 200-day average
DEV_START, DEV_END = date(2019, 1, 1), date(2023, 12, 31)
TEST_START = date(2024, 1, 1)

ETFS = ["QQQ", "IWM", "DIA", "XLK", "XLF", "XLV", "XLE", "XLI", "XLY", "XLP", "XLU", "XLB"]
# About 45 of the largest S&P 500 stocks today. Picking today's winners is a
# bias (survivorship): these all did well enough to still be large. The bot's
# own AAPL, MSFT, NVDA and SPY are left out so positions never mix.
STOCKS = ["AMZN", "GOOGL", "META", "AVGO", "TSLA", "BRK.B", "JPM", "LLY", "V", "UNH", "XOM", "MA", "COST", "HD",
          "PG", "JNJ", "WMT", "NFLX", "ABBV", "BAC", "CRM", "ORCL", "KO", "CVX", "MRK", "AMD", "PEP", "TMO", "ADBE",
          "CSCO", "ACN", "LIN", "MCD", "ABT", "WFC", "DIS", "INTU", "IBM", "QCOM", "TXN", "GE", "CAT", "AMGN", "VZ",
          "ISRG"]

# --- 1. Dip buying in uptrends (daily) --------------------------------------
# After the close: buy at the next open if the close is above its 200-day
# average and the 2-day RSI is under 10. Sell at the next open once a close
# is above the 5-day average, or after DIP_MAX_DAYS days.
DIP_UNIVERSE = ETFS + STOCKS
DIP_TREND_DAYS = 200
DIP_RSI_DAYS = 2
DIP_RSI_BELOW = 10
DIP_EXIT_AVG_DAYS = 5
DIP_MAX_DAYS = 10
DIP_SLOTS = 3  # positions at once, SLEEVE / 3 each; the most oversold first
DIP_COST = 0.0003  # 0.03% a side: spread and slippage on market orders at the open

# --- 2. Price ladder (grid) on an S&P 500 ETF -------------------------------
# VOO, not SPY: the main bot trades SPY, and the account can't keep two bots'
# shares of one symbol apart. Same index.
GRID_SYMBOL = "VOO"
GRID_SLICES = 5  # SLEEVE / 5 each
GRID_STEP = 0.01  # buy a slice every 1% below the anchor
GRID_TAKE = 0.01  # sell each slice 1% above its own buy
# The anchor is the highest price since the ladder was last empty. Buy orders
# move up with it only once it has risen this much, so they aren't replaced
# every minute.
GRID_REANCHOR = 0.0025

# --- 3. Intraday stretch below VWAP (5-minute bars) -------------------------
# VWAP: the day's average price weighted by volume, from 9:30. Buy at the next
# bar's open when a 5-minute bar closes more than STRETCH_BANDS standard
# deviations below it; sell when the price gets back to the VWAP at entry,
# at a stop the same distance below the entry, or at 3:55.
STRETCH_UNIVERSE = ["QQQ", "IWM", "DIA"] + ["AMZN", "GOOGL", "META", "AVGO", "TSLA", "JPM", "LLY", "V", "UNH",
                                            "XOM", "MA", "COST", "HD", "PG", "JNJ", "WMT", "NFLX", "ABBV", "BAC",
                                            "AMD"]
STRETCH_BAR_MINUTES = 5
STRETCH_BANDS = 2.0
STRETCH_FIRST_ENTRY = "10:00"  # the VWAP and its band need half an hour to settle
STRETCH_LAST_ENTRY = "15:30"
STRETCH_FLAT = "15:55"
STRETCH_STOP_MULT = 1.0  # stop distance = this x the distance to the target
STRETCH_COST = 0.0003  # 0.03% a side

# --- Paper trading (live.py) ------------------------------------------------
# Approved Nov 16, 2026: dip buying and the ladder, $2,500 each. The intraday
# stretch lost money in every year of the backtest and was turned down.
LIVE_SLEEVES = {"dip": 2_500, "grid": 2_500}
# The main bot's stocks (SYMBOLS in the top-level config.py): never traded here.
LIVE_EXCLUDE = ["AAPL", "MSFT", "NVDA", "SPY"]
PAPER_ACCOUNT_ID = "PA3KAMOZC"
MIN_EQUITY = 25_000  # refuse to run below this (pattern day trader rule)
POLL_SECONDS = 30  # how often the ladder checks its orders during the day
FILL_WAIT_SECONDS = 120  # how long the dip orders at the open get to fill
