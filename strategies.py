"""Trading strategies shared by the live bot and the backtester.

Each strategy takes a Series of daily closing prices and returns a Series with
1 (hold the stock) or 0 (stay in cash) for every day, decided at that day's
close. Settings live in config.py.
"""

import pandas as pd

import config


def ma_crossover(close, next_day=None):
    """Hold while the short moving average is above the long one."""
    short_ma = close.rolling(config.SHORT_WINDOW).mean()
    long_ma = close.rolling(config.LONG_WINDOW).mean()
    return (short_ma > long_ma).astype(int)


def rsi(close, period):
    change = close.diff()
    gain = change.clip(lower=0).ewm(alpha=1 / period, min_periods=period).mean()
    loss = (-change.clip(upper=0)).ewm(alpha=1 / period, min_periods=period).mean()
    return 100 - 100 / (1 + gain / loss)


def rsi_reversion(close, next_day=None):
    """Buy when RSI drops below the buy level (oversold), sell when it rises above the sell level."""
    position, holding = [], 0
    for value in rsi(close, config.RSI_PERIOD):
        if not holding and value < config.RSI_BUY_BELOW:
            holding = 1
        elif holding and value > config.RSI_SELL_ABOVE:
            holding = 0
        position.append(holding)
    return pd.Series(position, index=close.index)


def momentum(close, next_day=None):
    """On the last trading day of each month: hold if the stock is up over the lookback period.

    `next_day` is the date of the trading day after the last close. The live
    bot passes today's date so it can tell whether yesterday ended a month.
    """
    past = close.shift(config.MOMENTUM_LOOKBACK)
    trending_up = (close > past).astype(float).where(past.notna())

    dates = close.index.to_series()
    following = dates.shift(-1)
    if next_day is not None:
        following.iloc[-1] = pd.Timestamp(next_day, tz=dates.iloc[-1].tz)
    month_end = following.notna() & (dates.dt.month != following.dt.month)
    return trending_up.where(month_end).ffill().fillna(0).astype(int)


def breakout(close, next_day=None):
    """Buy on a new N-day high, sell on a new M-day low."""
    entry_level = close.rolling(config.BREAKOUT_ENTRY).max().shift(1)
    exit_level = close.rolling(config.BREAKOUT_EXIT).min().shift(1)
    position, holding = [], 0
    for price, high, low in zip(close, entry_level, exit_level):
        if not holding and price > high:
            holding = 1
        elif holding and price < low:
            holding = 0
        position.append(holding)
    return pd.Series(position, index=close.index)


def describe(key, close):
    """One line on where a strategy's indicator stands at the latest close."""
    if key == "crossover":
        short_ma = close.rolling(config.SHORT_WINDOW).mean().iloc[-1]
        long_ma = close.rolling(config.LONG_WINDOW).mean().iloc[-1]
        return f"{config.SHORT_WINDOW}-day avg {short_ma:.2f} vs {config.LONG_WINDOW}-day avg {long_ma:.2f}"
    if key == "rsi":
        return f"RSI {rsi(close, config.RSI_PERIOD).iloc[-1]:.1f} (buy below {config.RSI_BUY_BELOW}, sell above {config.RSI_SELL_ABOVE})"
    if key == "momentum":
        change = close.iloc[-1] / close.iloc[-1 - config.MOMENTUM_LOOKBACK] - 1
        return f"{change:+.1%} over the last {config.MOMENTUM_LOOKBACK} trading days (checked at month end)"
    if key == "breakout":
        high = close.iloc[-1 - config.BREAKOUT_ENTRY:-1].max()
        low = close.iloc[-1 - config.BREAKOUT_EXIT:-1].min()
        return f"close {close.iloc[-1]:.2f}, {config.BREAKOUT_ENTRY}-day high {high:.2f}, {config.BREAKOUT_EXIT}-day low {low:.2f}"
    return ""


# key: (display name, function)
STRATEGIES = {
    "crossover": (f"MA crossover ({config.SHORT_WINDOW}/{config.LONG_WINDOW})", ma_crossover),
    "rsi": (f"RSI reversion ({config.RSI_BUY_BELOW}/{config.RSI_SELL_ABOVE})", rsi_reversion),
    "momentum": (f"Momentum ({config.MOMENTUM_LOOKBACK}-day)", momentum),
    "breakout": (f"Breakout ({config.BREAKOUT_ENTRY}/{config.BREAKOUT_EXIT})", breakout),
}
