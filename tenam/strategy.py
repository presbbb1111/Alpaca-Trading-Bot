"""The 10 AM break-and-retest model, one trading day at a time.

DayModel takes 1-minute bars one by one (on_bar), so the backtest and the live
paper bot share the same logic. run_day() feeds it a whole day of history.

Prices here are raw: no slippage. backtest.py applies costs.

Rules (all numbers in config.py):
- Opening range: high and low of the 1-min bars 09:30 to 09:59. The day is
  skipped if the range is wider than MAX_OR_MULT x the median of the previous
  OR_MEDIAN_DAYS ranges (and when fewer days than that exist).
- Break: a 5-min bar (10:00-10:04, 10:05-10:09, ...) that closes above the
  range high (long) or below the range low (short).
- Retest: a later 1-min bar whose low reaches the range high + RETEST_TOL (or
  lower) and that closes back above the range high. Short is the mirror.
- Entry: the open of the next 1-min bar, if that is no later than LAST_ENTRY.
- Cancel: a 1-min bar closing more than INVALIDATE_PCT of the range back
  inside it before a retest. A later break in that direction starts a new setup.
- Stop: one tick beyond the retest bar's extreme, at least MIN_STOP_PCT of the
  entry and MIN_STOP_OR_FRAC of the range width away. Target: R_MULT x the risk. Stop and target in the same 1-min bar
  counts as the stop. A stop gapped through fills at the bar's open.
- Time exit: anything open exits at the close of the FLAT_TIME bar (5 minutes
  before the close on early-close days).
- One position at a time, at most MAX_TRADES a day. Entering clears every
  pending setup; after an exit, a new trade needs a fresh break from a 5-min
  bar that starts after the exit.
"""

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

import pandas as pd

import config


@dataclass
class Params:
    or_end: time = config.OR_END
    max_or_mult: float = config.MAX_OR_MULT
    retest_tol: float = config.RETEST_TOL
    invalidate_pct: float = config.INVALIDATE_PCT
    last_entry: time = config.LAST_ENTRY
    stop_buffer: float = config.STOP_BUFFER
    min_stop_pct: float = config.MIN_STOP_PCT
    min_stop_or_frac: float = config.MIN_STOP_OR_FRAC
    r_mult: float = config.R_MULT
    flat_time: time = config.FLAT_TIME
    max_trades: int = config.MAX_TRADES
    break_minutes: int = config.BREAK_BAR_MINUTES


@dataclass
class Trade:
    side: str  # "long" or "short"
    trade_no: int
    break_time: datetime
    retest_time: datetime
    entry_time: datetime
    entry: float
    stop: float
    target: float
    risk: float  # entry to stop, per share
    stop_floored: bool  # the stop was widened to the minimum distance
    exit_time: datetime = None
    exit_price: float = None
    exit_kind: str = None  # "target", "stop", "time"

    @property
    def sign(self):
        return 1 if self.side == "long" else -1

    @property
    def gross_r(self):
        return self.sign * (self.exit_price - self.entry) / self.risk


@dataclass
class Setup:
    side: str
    break_time: datetime  # start of the 5-min break bar
    armed_at: datetime  # end of the break bar; retests come after this


@dataclass
class DayResult:
    or_high: float = None
    or_low: float = None
    or_width: float = None
    median_width: float = None
    skipped: str = None  # why the day was skipped, if it was
    trades: list = field(default_factory=list)
    breaks: list = field(default_factory=list)  # (time, side, 5-min close)
    retests: list = field(default_factory=list)  # (time, side)
    cancels: list = field(default_factory=list)  # (time, side)


class DayModel:
    """Feed 1-min bars in time order with on_bar(); read .result as you go."""

    def __init__(self, median_width, session_close=time(16, 0), params=None):
        self.p = params or Params()
        self.median_width = median_width
        self.result = DayResult(median_width=median_width)
        close_minus_5 = (datetime.combine(datetime.min, session_close) - timedelta(minutes=5)).time()
        self.flat_time = min(self.p.flat_time, close_minus_5)
        self.or_bars = []
        self.range_ready = False
        self.setups = {}  # side -> Setup
        self.position = None
        self.pending_entry = None  # (side, Setup, retest bar) waiting for the next bar's open
        self.last_exit = None
        self.bucket = None  # current 5-min bucket start
        self.bucket_close = None
        self.done = False

    # --- helpers ---
    def _bucket_of(self, ts):
        return ts.replace(minute=ts.minute - ts.minute % self.p.break_minutes, second=0, microsecond=0)

    def _finish_range(self):
        r = self.result
        if not self.or_bars:
            r.skipped = "no opening-range bars"
            self.done = True
            return
        r.or_high = max(b["high"] for b in self.or_bars)
        r.or_low = min(b["low"] for b in self.or_bars)
        r.or_width = r.or_high - r.or_low
        self.range_ready = True
        if self.median_width is None:
            r.skipped = "warm-up: under 20 earlier days for the median range"
            self.done = True
        elif r.or_width > self.p.max_or_mult * self.median_width:
            r.skipped = f"range {r.or_width:.2f} > {self.p.max_or_mult:g} x median {self.median_width:.2f}"
            self.done = True

    def _close_bucket(self):
        """A 5-min bar just finished: look for a break."""
        start, close = self.bucket, self.bucket_close
        self.bucket = None
        if start is None or self.position or self.pending_entry or self.done:
            return
        if len(self.result.trades) >= self.p.max_trades or start.time() > self.p.last_entry:
            return
        if self.last_exit is not None and start < self.last_exit:
            return  # needs a fresh break that starts after the last exit
        r = self.result
        armed_at = start + timedelta(minutes=self.p.break_minutes)
        for side, broke in (("long", close > r.or_high), ("short", close < r.or_low)):
            if broke and side not in self.setups:
                self.setups[side] = Setup(side, start, armed_at)
                r.breaks.append((start, side, close))

    def levels(self, entry):
        """Stop, risk, target and whether the stop was widened, for the pending
        entry at this entry price (None if the price is already past the stop).
        The live bot calls this with its best guess of the fill."""
        side, _, retest = self.pending_entry
        if side == "long":
            stop = retest["low"] - self.p.stop_buffer
            risk = entry - stop
        else:
            stop = retest["high"] + self.p.stop_buffer
            risk = stop - entry
        if risk <= 0:
            return None
        floor = max(self.p.min_stop_pct * entry, self.p.min_stop_or_frac * self.result.or_width)
        floored = risk < floor
        if floored:
            risk = floor
            stop = entry - risk if side == "long" else entry + risk
        target = entry + self.p.r_mult * risk if side == "long" else entry - self.p.r_mult * risk
        return stop, risk, target, floored

    def _enter(self, bar):
        side, setup, retest = self.pending_entry
        ts, entry = bar["timestamp"], bar["open"]
        levels = self.levels(entry)
        self.pending_entry = None
        if ts.time() > self.p.last_entry or len(self.result.trades) >= self.p.max_trades:
            return
        if levels is None:
            return  # opened beyond the stop: no trade
        stop, risk, target, floored = levels
        self.position = Trade(
            side, len(self.result.trades) + 1, setup.break_time, retest["timestamp"], ts,
            entry, stop, target, risk, floored,
        )
        self.result.trades.append(self.position)
        self.setups.clear()

    def _manage(self, bar):
        t = self.position
        ts = bar["timestamp"]
        if t.side == "long":
            hit_stop, hit_target = bar["low"] <= t.stop, bar["high"] >= t.target
            gapped = bar["open"] <= t.stop
        else:
            hit_stop, hit_target = bar["high"] >= t.stop, bar["low"] <= t.target
            gapped = bar["open"] >= t.stop
        if hit_stop:  # also when the target hit in the same bar: scored as a loss
            self._exit(ts, bar["open"] if gapped else t.stop, "stop")
        elif hit_target:
            self._exit(ts, t.target, "target")
        elif ts.time() >= self.flat_time:
            self._exit(ts, bar["close"], "time")

    def _exit(self, ts, price, kind):
        t = self.position
        t.exit_time, t.exit_price, t.exit_kind = ts, price, kind
        self.position = None
        self.last_exit = ts + timedelta(minutes=1)  # the end of the exit bar

    def _watch_setups(self, bar):
        r = self.result
        tol = self.p.retest_tol * r.or_high
        cancel_by = self.p.invalidate_pct * r.or_width
        for side in ("long", "short"):
            s = self.setups.get(side)
            if s is None or bar["timestamp"] < s.armed_at:
                continue
            if side == "long":
                retest = bar["low"] <= r.or_high + tol and bar["close"] > r.or_high
                cancel = bar["close"] < r.or_high - cancel_by
            else:
                tol_s = self.p.retest_tol * r.or_low
                retest = bar["high"] >= r.or_low - tol_s and bar["close"] < r.or_low
                cancel = bar["close"] > r.or_low + cancel_by
            if retest:
                r.retests.append((bar["timestamp"], side))
                self.pending_entry = (side, s, bar)
                return
            if cancel:
                r.cancels.append((bar["timestamp"], side))
                del self.setups[side]

    # --- main entry point ---
    def on_bar(self, bar):
        """bar: mapping with timestamp (NY time), open, high, low, close."""
        ts = bar["timestamp"]
        t = ts.time()
        if not self.range_ready and not self.done:
            if t < self.p.or_end:
                self.or_bars.append(bar)
                return
            self._finish_range()
        if self.done and self.position is None:
            return

        b = self._bucket_of(ts)
        if self.bucket is not None and b != self.bucket:
            self._close_bucket()  # a bar was missing at the end of the last bucket

        if self.pending_entry:
            self._enter(bar)
        if self.position:
            self._manage(bar)
        elif not self.pending_entry and t <= self.p.last_entry:
            self._watch_setups(bar)

        if t >= self.p.or_end:
            self.bucket, self.bucket_close = b, bar["close"]
            if (ts.minute + 1) % self.p.break_minutes == 0:
                self._close_bucket()

    def finish(self, last_bar=None):
        """End of data: exit anything still open at the last bar's close."""
        if self.position and last_bar is not None:
            self._exit(last_bar["timestamp"], last_bar["close"], "time")
        return self.result


def run_day(bars, median_width, session_close=time(16, 0), params=None):
    """bars: one day's 1-min bars (DataFrame with timestamp, open, high, low, close)."""
    model = DayModel(median_width, session_close, params)
    last = None
    for bar in bars[["timestamp", "open", "high", "low", "close"]].to_dict("records"):
        model.on_bar(bar)
        last = bar
    return model.finish(last)


def median_widths(bars, days=config.OR_MEDIAN_DAYS, or_end=config.OR_END):
    """Median opening-range width of the previous `days` sessions, by date."""
    or_bars = bars[bars["timestamp"].dt.time < or_end]
    widths = or_bars.groupby("date").agg(h=("high", "max"), l=("low", "min"))
    widths = widths["h"] - widths["l"]
    return widths.shift(1).rolling(days, min_periods=days).median()


def r_with_costs(trade, slip=config.SLIP_PER_SHARE):
    """R after slippage on entry and exit (each against us)."""
    s = trade.sign
    entry_fill = trade.entry + s * slip
    exit_fill = trade.exit_price - s * slip
    return s * (exit_fill - entry_fill) / trade.risk
