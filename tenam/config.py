"""10 AM model settings. Every number the backtest and the live bot use is here."""

from datetime import date, time

# --- Data ---
SYMBOLS = ["QQQ", "SPY"]  # ETF stand-ins for NQ and ES futures (Alpaca has no futures)
MAIN_SYMBOL = "QQQ"
DATA_START = date(2018, 11, 1)  # Nov-Dec 2018 only feeds the 20-day median range for early 2019
FEED = "sip"  # falls back to "iex" if the plan refuses SIP

# --- Periods ---
DEV_START, DEV_END = date(2019, 1, 1), date(2023, 12, 31)
HOLDOUT_START = date(2024, 1, 1)  # through the latest cached day
VIDEO_WINDOW_END = date(2026, 7, 31)  # the 90 trading days before this date
VIDEO_WINDOW_DAYS = 90

# --- Opening range (2.1) ---
OR_START = time(9, 30)
OR_END = time(10, 0)  # range is the 1-min bars from 09:30 to 09:59
MAX_OR_MULT = 2.0  # skip the day if the range is wider than this x the 20-day median
OR_MEDIAN_DAYS = 20

# --- Break, retest, entry (2.2, 2.3) ---
BREAK_BAR_MINUTES = 5  # a break is a 5-min bar closing outside the range
RETEST_TOL = 0.0005  # 0.05% of price
INVALIDATE_PCT = 0.25  # cancel if a 1-min bar closes this far back inside the range
LAST_ENTRY = time(11, 30)  # no entries after this time

# --- Stop, target, exits (2.4) ---
STOP_BUFFER = 0.01  # one tick beyond the retest bar's extreme
# Stop distance is at least this fraction of price. Phase 2 used 0.05%; at that size
# slippage cost ~0.11R a trade. Change 1 (approved): 0.15%, frozen for the holdout.
MIN_STOP_PCT = 0.0015
MIN_STOP_OR_FRAC = 0.0  # ...and at least this fraction of the opening range's width (0 = off)
R_MULT = 4.0
FLAT_TIME = time(15, 55)  # anything still open exits at this bar's close
MAX_TRADES = 5

# --- Costs (2.5) ---
SLIP_PER_SHARE = 0.01  # on entry and on exit, always against us
COMMISSION = 0.0  # dollars per trade side

# --- Sizing (2.6) ---
START_EQUITIES = [10_000, 50_000]
RISK_PCT = 0.01

# --- Live paper bot (Phase 4: live_paper.py) ---
PAPER_ACCOUNT_ID = "PA3KAMOZC"  # the bot refuses to run on any other account
LIVE_SYMBOL = "QQQ"
# The free data plan streams only IEX bars live (one exchange). The 20-day
# median range still comes from SIP history; weekly_report.py compares with SIP.
LIVE_FEED = "iex"
# The account is shared with the other bots, so trades are small: R is what
# we're measuring. Shares = the fewer of LIVE_RISK_DOLLARS / stop distance and
# LIVE_MAX_NOTIONAL / price. At QQQ ~$680 and a 0.15% stop: 36 shares, ~$37 at risk.
LIVE_RISK_DOLLARS = 100
LIVE_MAX_NOTIONAL = 25_000
MAX_DAILY_LOSS_R = 3  # no new trades once the day's closed trades reach -3R
MIN_EQUITY = 25_000  # stay clear of the pattern-day-trader rule
