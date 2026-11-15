"""Hand-built days for strategy.py. Run from tenam/: python -m pytest -q

Every day has the same opening range (100.00 to 101.00, width 1.00) and a
20-day median width of 1.00 unless a test says otherwise. Bars not listed are
flat at the day's filler price.
"""

import sys
from datetime import datetime, time
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strategy import Params, r_with_costs  # noqa: E402
from strategy import run_day as _run_day  # noqa: E402


def run_day(bars, median_width, params=None):
    """The hand-built prices below assume the original 0.05% minimum stop."""
    return _run_day(bars, median_width, params=params or Params(min_stop_pct=0.0005))

DAY = datetime(2021, 3, 3)


def ts(hhmm):
    h, m = map(int, hhmm.split(":"))
    return pd.Timestamp(DAY.replace(hour=h, minute=m)).tz_localize("America/New_York")


def make_day(bars, filler=100.5):
    """bars: {"HH:MM": (open, high, low, close)}; everything else is filler."""
    rows = []
    for i in range(390):
        t = ts("09:30") + pd.Timedelta(minutes=i)
        key = t.strftime("%H:%M")
        if key in bars:
            o, h, l, c = bars[key]
        elif t.time() < time(10, 0):  # opening range: 100.00 to 101.00
            o, h, l, c = 100.5, 101.0 if i % 2 else 100.6, 100.0 if i % 2 == 0 else 100.4, 100.5
        else:
            o = h = l = c = filler
        rows.append({"timestamp": t, "open": o, "high": h, "low": l, "close": c})
    return pd.DataFrame(rows)


# A bullish break: the 10:00-10:04 5-min bar closes at 101.50, above 101.00.
BREAK_UP = {f"10:0{m}": (101.2, 101.5, 101.2, 101.2 + 0.075 * m) for m in range(5)}
# A retest: the low comes back to 101.03 (within 0.05% of 101.00) and closes
# back above at 101.20. Stop 101.02; entry at the next open, 101.20; risk 0.18;
# target 101.20 + 4 x 0.18 = 101.92.
RETEST_UP = {"10:05": (101.4, 101.4, 101.03, 101.2), "10:06": (101.2, 101.4, 101.2, 101.4)}

BREAK_DOWN = {f"10:0{m}": (99.8, 99.8, 99.5, 99.8 - 0.075 * m) for m in range(5)}
RETEST_DOWN = {"10:05": (99.6, 99.97, 99.6, 99.8), "10:06": (99.8, 99.8, 99.6, 99.6)}


def one_trade(result):
    assert result.skipped is None
    assert len(result.trades) == 1
    return result.trades[0]


def test_clean_long_win():
    day = make_day({**BREAK_UP, **RETEST_UP, "10:07": (101.5, 102.0, 101.5, 101.9)})
    t = one_trade(run_day(day, median_width=1.0))
    assert t.side == "long"
    assert t.break_time == ts("10:00") and t.retest_time == ts("10:05")
    assert t.entry_time == ts("10:06") and t.entry == 101.2
    assert t.stop == pytest.approx(101.02) and t.risk == pytest.approx(0.18)
    assert t.target == pytest.approx(101.92)
    assert t.exit_kind == "target" and t.exit_time == ts("10:07")
    assert t.gross_r == pytest.approx(4.0)
    # $0.01 slippage each way: (101.91 - 101.21) / 0.18
    assert r_with_costs(t, 0.01) == pytest.approx(0.70 / 0.18)


def test_clean_short_loss():
    day = make_day({**BREAK_DOWN, **RETEST_DOWN, "10:07": (99.85, 100.1, 99.8, 100.0)})
    t = one_trade(run_day(day, median_width=1.0))
    assert t.side == "short"
    assert t.entry == 99.8 and t.stop == pytest.approx(99.98) and t.target == pytest.approx(99.08)
    assert t.exit_kind == "stop" and t.exit_price == pytest.approx(99.98)
    assert t.gross_r == pytest.approx(-1.0)
    assert r_with_costs(t, 0.01) == pytest.approx(-0.20 / 0.18)


def test_stop_and_target_in_same_bar_is_a_loss():
    day = make_day({**BREAK_UP, **RETEST_UP, "10:07": (101.5, 102.0, 101.0, 101.5)})
    t = one_trade(run_day(day, median_width=1.0))
    assert t.exit_kind == "stop"
    assert t.gross_r == pytest.approx(-1.0)


def test_time_exit_at_1555_close():
    day = make_day({**BREAK_UP, **RETEST_UP, "15:55": (101.4, 101.5, 101.3, 101.45)}, filler=101.4)
    t = one_trade(run_day(day, median_width=1.0))
    assert t.exit_kind == "time" and t.exit_time == ts("15:55")
    assert t.exit_price == 101.45
    assert t.gross_r == pytest.approx(0.25 / 0.18)  # a fractional R


def test_no_break_day():
    result = run_day(make_day({}), median_width=1.0)
    assert result.skipped is None
    assert result.breaks == [] and result.trades == []


def test_oversized_opening_range_skips_the_day():
    day = make_day({**BREAK_UP, **RETEST_UP, "10:07": (101.5, 102.0, 101.5, 101.9)})
    result = run_day(day, median_width=0.4)  # 1.00 > 2 x 0.40
    assert result.skipped.startswith("range")
    assert result.trades == []


def test_warm_up_day_without_a_median_is_skipped():
    result = run_day(make_day(BREAK_UP), median_width=None)
    assert result.skipped.startswith("warm-up") and result.trades == []


def test_close_back_inside_cancels_the_setup():
    # 10:05 closes at 100.70: more than 25% of the range (0.25) back below 101.00.
    day = make_day({**BREAK_UP, "10:05": (101.0, 101.0, 100.6, 100.7), "10:06": (101.4, 101.4, 101.03, 101.2)})
    result = run_day(day, median_width=1.0)
    assert result.cancels == [(ts("10:05"), "long")]
    assert result.trades == []


def test_no_entry_after_last_entry_time():
    late_break = {f"11:2{m}": (101.2, 101.5, 101.2, 101.5) for m in range(5, 10)}
    day = make_day({**late_break, "11:30": (101.4, 101.4, 101.03, 101.2)})
    result = run_day(day, median_width=1.0)
    assert result.retests == [(ts("11:30"), "long")]  # entry would be 11:31
    assert result.trades == []


def test_tight_stop_is_widened_to_the_minimum():
    # Retest low 101.04 -> stop 101.03; entry 101.06 -> risk 0.03, under 0.05% (0.0505).
    day = make_day({**BREAK_UP, "10:05": (101.3, 101.3, 101.04, 101.2), "10:06": (101.06, 101.3, 101.06, 101.3)})
    t = one_trade(run_day(day, median_width=1.0))
    assert t.stop_floored
    assert t.risk == pytest.approx(0.0005 * 101.06)


def test_minimum_stop_from_the_range_width():
    # Natural risk 0.18; a quarter of the 1.00 range is 0.25, so the stop widens to 0.25.
    day = make_day({**BREAK_UP, **RETEST_UP})
    t = one_trade(run_day(day, median_width=1.0, params=Params(min_stop_or_frac=0.25)))
    assert t.stop_floored and t.risk == pytest.approx(0.25)
    assert t.stop == pytest.approx(100.95) and t.target == pytest.approx(102.2)


def test_stop_gapped_through_fills_at_the_open():
    day = make_day({**BREAK_UP, **RETEST_UP, "10:07": (100.9, 101.0, 100.8, 100.9)})
    t = one_trade(run_day(day, median_width=1.0))
    assert t.exit_kind == "stop" and t.exit_price == 100.9
    assert t.gross_r == pytest.approx(-0.30 / 0.18)


def test_new_trade_needs_a_fresh_break_after_a_loss():
    bars = {**BREAK_UP, **RETEST_UP, "10:07": (101.2, 101.2, 101.0, 101.1)}  # stopped at 101.02
    # 10:08-10:09 stay above the range, so the 10:05 bucket closes above it,
    # but it started before the exit: no new setup from it.
    bars.update({"10:08": (101.3, 101.3, 101.3, 101.3), "10:09": (101.3, 101.3, 101.3, 101.3)})
    # 10:10-10:14: a fresh 5-min close above, then a retest at 10:15.
    bars.update({f"10:1{m}": (101.4, 101.5, 101.4, 101.5) for m in range(5)})
    bars.update({"10:15": (101.4, 101.4, 101.02, 101.2), "10:16": (101.2, 101.3, 101.2, 101.3)})
    result = run_day(make_day(bars), median_width=1.0)
    assert [b[0] for b in result.breaks] == [ts("10:00"), ts("10:10")]
    assert len(result.trades) == 2
    assert result.trades[1].trade_no == 2 and result.trades[1].entry_time == ts("10:16")
