"""live_paper.LiveBot against a fake broker. Run from tenam/: python -m pytest -q

Uses the hand-built days from test_strategy.py (range 100.00-101.00). The
long entered at 101.20 has its stop at 101.02 (0.18 away, wider than the
0.15% minimum of 0.1518) and its target at 101.92.
"""

import sys
from datetime import date, time, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import live_paper  # noqa: E402
from live_paper import FakeBroker, LiveBot, replay  # noqa: E402
from strategy import r_with_costs, run_day  # noqa: E402
from test_strategy import BREAK_UP, RETEST_UP, make_day, ts  # noqa: E402

DAY = date(2021, 3, 3)


def bars_of(day_df):
    return day_df[["timestamp", "open", "high", "low", "close"]].to_dict("records")


def new_bot(tmp_path, broker=None, **kw):
    broker = broker or FakeBroker()
    bot = LiveBot(DAY, "QQQ", broker, 1.0, time(16, 0), alerts=False, csv_path=tmp_path / "t.csv",
                  sleep=lambda s: None, **kw)
    return bot, broker


WIN = {**BREAK_UP, **RETEST_UP, "10:07": (101.5, 102.0, 101.5, 101.9)}  # reaches the 101.92 target


def test_live_trade_matches_the_backtest(tmp_path):
    day = make_day(WIN)
    bot, broker = new_bot(tmp_path)
    replay(bot, broker, bars_of(day))
    model = run_day(day, 1.0)
    assert len(model.trades) == 1 and len(bot.signals) == 1
    sig, t = bot.signals[0], model.trades[0]
    assert sig.status == "closed" and sig.client_id == "tenam-20210303-QQQ-1"
    assert sig.entry_fill == t.entry == 101.2
    assert sig.exit_kind == t.exit_kind == "target"
    assert sig.order_target == pytest.approx(round(t.target, 2))
    assert sig.live_r == pytest.approx(t.gross_r, abs=0.02)  # cents rounding of the bracket
    assert sig.shares == 247  # $25,000 / 101.20; $100 / 0.18 would be 555
    row = bot.rows()[0]
    assert row["intended_r"] == pytest.approx(r_with_costs(t)) and row["entry_slippage"] == 0


def test_time_exit_closes_at_1555(tmp_path):
    day = make_day({**BREAK_UP, **RETEST_UP}, filler=101.4)
    bot, broker = new_bot(tmp_path)
    replay(bot, broker, bars_of(day))
    sig = bot.signals[0]
    assert sig.status == "closed" and sig.exit_kind == "time"
    assert sig.exit_fill_time == ts("15:55")  # sent at 15:55, filled at that bar's open
    assert broker.position_qty("QQQ") == 0
    assert all(leg["status"] == "canceled" for leg in broker.orders[sig.client_id]["legs"])


def test_restart_does_not_resend_an_order(tmp_path):
    bars = bars_of(make_day(WIN))
    broker = FakeBroker()
    first, _ = new_bot(tmp_path, broker)
    replay(first, broker, bars[:37])  # up to 10:06: the order is in
    assert first.signals[0].status in ("submitted", "filled")
    second, _ = new_bot(tmp_path, broker)  # same broker: the order already exists
    for bar in bars[:37]:
        second.on_bar(bar, bars[36]["timestamp"] + timedelta(minutes=1, seconds=2))
    assert second.signals[0].status == "submitted"
    assert len([k for k in broker.orders if not k.endswith("-flat")]) == 1


def test_signals_from_before_the_bot_started_are_not_traded(tmp_path):
    bars = bars_of(make_day(WIN))
    bot, broker = new_bot(tmp_path)
    late = bars[-1]["timestamp"]
    for bar in bars[:40]:
        bot.on_bar(bar, late)  # catching up at the end of the day
    assert bot.signals[0].status == "missed" and broker.orders == {}


def test_skips_when_the_account_already_holds_the_symbol(tmp_path):
    broker = FakeBroker()
    broker.pos = 10  # another bot's shares
    bot, _ = new_bot(tmp_path, broker)
    replay(bot, broker, bars_of(make_day(WIN)))
    assert bot.signals[0].status == "skipped" and "already holds" in bot.signals[0].reason
    assert broker.position_qty("QQQ") == 10  # untouched at the close-out


def test_daily_loss_limit_stops_new_trades(tmp_path):
    bot, broker = new_bot(tmp_path)
    for n, r in enumerate((-1.1, -1.1, -1.1), start=1):
        s = live_paper.Signal(n, "long", ts("10:05"), status="closed", order_risk=1.0, entry_fill=100.0,
                              exit_fill=100.0 + r)
        bot.signals.append(s)
    replay(bot, broker, bars_of(make_day(WIN)))
    assert bot.signals[-1].status == "skipped" and "daily loss" in bot.signals[-1].reason
    assert broker.orders == {}


def test_kill_flattens_an_open_trade(tmp_path):
    bars = bars_of(make_day({**BREAK_UP, **RETEST_UP}, filler=101.4))
    bot, broker = new_bot(tmp_path)
    replay_part = bars[:45]
    for bar in replay_part:
        now = bar["timestamp"] + timedelta(minutes=1, seconds=2)
        broker.on_bar(bar)
        bot.poll(now)
        bot.on_bar(bar, now)
    assert broker.position_qty("QQQ") == 247
    bot.halted = "KILL file"
    bot.flatten("KILL file")
    broker.on_bar(bars[45])
    bot.poll(bars[45]["timestamp"] + timedelta(minutes=1))
    assert broker.position_qty("QQQ") == 0 and bot.signals[0].exit_kind == "time"
    assert bot.settled()


def test_dry_run_sends_nothing(tmp_path):
    bot, broker = new_bot(tmp_path, dry_run=True)
    replay(bot, broker, bars_of(make_day(WIN)))
    assert bot.signals[0].status == "dry run" and broker.orders == {}
