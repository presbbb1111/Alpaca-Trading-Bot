"""live.Bot against a fake broker. Run from buylow/: python -m pytest -q"""

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402
import live  # noqa: E402
from live import Bot  # noqa: E402

ET = ZoneInfo("America/New_York")
DAY = date(2026, 11, 18)


class FakeBroker:
    def __init__(self):
        self.orders, self.pos, self.price, self.others, self.highs, self.rows = {}, {}, {}, set(), [], {}

    def submit(self, symbol, side, client_id, qty=None, notional=None, limit=None):
        assert client_id not in self.orders, "the same order was sent twice"
        self.orders[client_id] = {"symbol": symbol, "side": side, "qty": qty, "notional": notional, "limit": limit,
                                  "status": "new", "filled_qty": 0.0, "price": 0.0}

    def order(self, client_id):
        return self.orders.get(client_id)

    def cancel(self, client_id):
        o = self.orders[client_id]
        if o["status"] == "new":
            o["status"] = "canceled"

    def positions(self):
        return dict(self.pos)

    def others_orders(self):
        return set(self.others)

    def prices(self, symbols):
        return {s: self.price[s] for s in symbols if s in self.price}

    def highs_since(self, symbol, since):
        out = [(t, h) for t, h in self.highs if t > since]
        return out

    def daily_rows(self, symbols, last_close):
        return dict(self.rows)

    def _fill(self, o, qty, price):
        o.update(status="filled", filled_qty=qty, price=price)
        sign = 1 if o["side"] == "buy" else -1
        self.pos[o["symbol"]] = self.pos.get(o["symbol"], 0.0) + sign * qty

    def fill_market(self):
        for o in self.orders.values():
            if o["status"] == "new" and o["limit"] is None:
                p = self.price[o["symbol"]]
                self._fill(o, o["qty"] if o["qty"] else o["notional"] / p, p)

    def trade(self, symbol, low, high):
        """The price ranged from low to high: limit orders traded through by a cent fill at their limit."""
        for o in self.orders.values():
            if o["status"] != "new" or o["limit"] is None or o["symbol"] != symbol:
                continue
            if (o["side"] == "buy" and low <= o["limit"] - 0.01) or (o["side"] == "sell" and high >= o["limit"] + 0.01):
                self._fill(o, o["qty"], o["limit"])


def calendar():
    days = pd.bdate_range("2026-10-01", "2026-11-30")
    return pd.DataFrame({"date": days})


def clock(start="2026-11-18 09:31"):
    t = {"now": datetime.fromisoformat(start).replace(tzinfo=ET)}

    def now():
        return t["now"]

    def sleep(seconds):
        t["now"] = t["now"] + pd.Timedelta(seconds=seconds)

    return now, sleep


def new_bot(tmp_path, broker):
    now, sleep = clock()
    return Bot(broker, DAY, tmp_path / "state.json", calendar(), alerts=False, sleep=sleep, now=now)


@pytest.fixture(autouse=True)
def quiet_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(live, "LOGS", tmp_path / "logs")


def row(close, rsi=50.0, buy=False, bounced=False):
    return {"close": close, "rsi": rsi, "buy": buy, "bounced": bounced, "trend": close * 0.9}


# --- Dip ---------------------------------------------------------------------

def test_dip_sells_the_bounce_and_buys_the_most_oversold(tmp_path):
    b = FakeBroker()
    b.rows = {s: row(100) for s in config.DIP_UNIVERSE}
    b.rows["KO"] = row(70, bounced=True)  # held, bounced: sell
    b.rows["JPM"] = row(200, rsi=3, buy=True)
    b.rows["XOM"] = row(100, rsi=6, buy=True)
    b.rows["PG"] = row(150, rsi=1, buy=True)  # most oversold, but another bot holds it
    b.rows["CVX"] = row(100, rsi=8, buy=True)
    b.pos = {"KO": 10.0, "PG": 5.0}
    b.price = {"KO": 71.0, "JPM": 201.0, "XOM": 99.0, "CVX": 100.0}
    bot = new_bot(tmp_path, b)
    bot.state["dip"].update(cash=1800.0, held={"KO": {"qty": 10.0, "entry": 70.0, "entry_date": "2026-11-16"}})
    bot.save()
    b.fill_market = b.fill_market  # fills happen when the bot waits
    orig_order = b.order
    b.order = lambda cid: (b.fill_market(), orig_order(cid))[1]
    bot.dip_open()
    d = bot.state["dip"]
    assert "KO" not in d["held"] and b.pos["KO"] == 0
    assert set(d["held"]) == {"JPM", "XOM", "CVX"}  # PG skipped; three slots
    value = 1800 + 10 * 71  # after the sale
    assert d["held"]["JPM"]["qty"] * 201 == pytest.approx(value / 3, abs=0.01)
    assert d["cash"] == pytest.approx(value - 3 * live.floor_to(value / 3, 2), abs=0.01)
    assert d["done"] == str(DAY)
    sent = len(b.orders)
    bot.dip_open()  # a restart the same day does nothing
    assert len(b.orders) == sent


def test_dip_sells_after_ten_days_and_counts_market_days(tmp_path):
    b = FakeBroker()
    b.rows = {s: row(100) for s in config.DIP_UNIVERSE}
    b.pos = {"KO": 1.0}
    b.price = {"KO": 95.0}
    bot = new_bot(tmp_path, b)
    bot.state["dip"]["held"] = {"KO": {"qty": 1.0, "entry": 100.0, "entry_date": "2026-11-04"}}
    assert bot.days_held("2026-11-04", date(2026, 11, 17)) == 10
    orig = b.order
    b.order = lambda cid: (b.fill_market(), orig(cid))[1]
    bot.dip_open()
    assert bot.state["dip"]["held"] == {} and bot.state["dip"]["cash"] == pytest.approx(2500 + 95)


def test_dip_skips_a_day_without_fresh_closes(tmp_path):
    b = FakeBroker()
    b.rows = {"KO": row(70, rsi=1, buy=True)}
    bot = new_bot(tmp_path, b)
    bot.dip_open()
    assert b.orders == {} and bot.state["dip"]["done"] == str(DAY)


# --- Ladder ------------------------------------------------------------------

def test_ladder_buys_sells_and_rebuys(tmp_path):
    b = FakeBroker()
    b.price = {"VOO": 600.0}
    bot = new_bot(tmp_path, b)
    bot.grid_start()
    buys = sorted(o["limit"] for o in b.orders.values())
    assert buys == [570.0, 576.0, 582.0, 588.0, 594.0]
    assert all(o["qty"] * o["limit"] <= 500 for o in b.orders.values())
    b.trade("VOO", 593.5, 600)  # slice 1 fills at 594
    bot.grid_poll()
    s1 = bot.grid.slices[0]
    assert s1.held and s1.buy_price == 594.0
    sells = [o for o in b.orders.values() if o["side"] == "sell" and o["status"] == "new"]
    assert len(sells) == 1 and sells[0]["limit"] == 599.94 and sells[0]["qty"] == pytest.approx(s1.qty)
    b.trade("VOO", 598, 600)
    bot.grid_poll()
    assert not s1.held
    assert bot.grid.cash == pytest.approx(2500 + s1_profit(b), abs=1e-6)
    working = [o for o in b.orders.values() if o["status"] == "new"]
    assert sorted(o["limit"] for o in working) == buys  # slice 1's buy is back


def s1_profit(b):
    buy = next(o for o in b.orders.values() if o["side"] == "buy" and o["status"] == "filled")
    sell = next(o for o in b.orders.values() if o["side"] == "sell" and o["status"] == "filled")
    return sell["filled_qty"] * sell["price"] - buy["filled_qty"] * buy["price"]


def test_ladder_restart_does_not_resend(tmp_path):
    b = FakeBroker()
    b.price = {"VOO": 600.0}
    bot = new_bot(tmp_path, b)
    bot.grid_start()
    again = new_bot(tmp_path, b)
    again.grid_start()
    again.grid_poll()
    assert len(b.orders) == 5


def test_ladder_moves_up_when_empty(tmp_path):
    b = FakeBroker()
    b.price = {"VOO": 600.0}
    bot = new_bot(tmp_path, b)
    bot.grid_start()
    b.highs = [(pd.Timestamp("2026-11-18 09:40", tz=ET), 601.0), (pd.Timestamp("2026-11-18 09:41", tz=ET), 602.0)]
    bot.grid_poll()  # cancels the old buys
    bot.grid_poll()  # places the new ones
    working = sorted(o["limit"] for o in b.orders.values() if o["status"] == "new")
    assert bot.grid.anchor == 602.0 and working == [571.9, 577.92, 583.94, 589.96, 595.98]


def test_ladder_partial_fill_then_expiry(tmp_path):
    b = FakeBroker()
    b.price = {"VOO": 600.0}
    bot = new_bot(tmp_path, b)
    bot.grid_start()
    cid = next(c for c, o in b.orders.items() if o["limit"] == 594.0)
    b.orders[cid].update(status="expired", filled_qty=0.3, price=594.0)
    bot.reconcile()
    s1 = bot.grid.slices[0]
    assert s1.qty == 0.3 and bot.grid.cash == pytest.approx(2500 - 0.3 * 594)


def test_stop_cancels_orders_and_keeps_holdings(tmp_path):
    b = FakeBroker()
    b.price = {"VOO": 600.0}
    bot = new_bot(tmp_path, b)
    bot.grid_start()
    b.trade("VOO", 593.5, 600)
    bot.grid_poll()
    bot.stop("KILL file")
    assert all(o["status"] in ("canceled", "filled") for o in b.orders.values())
    assert bot.grid.slices[0].held and bot.state["orders"] == {}


def test_end_of_day_writes_progress(tmp_path):
    b = FakeBroker()
    b.price = {"VOO": 600.0}
    bot = new_bot(tmp_path, b)
    bot.grid_start()
    b.trade("VOO", 593.5, 600)
    bot.grid_poll()
    b.price["VOO"] = 597.0
    bot.end_of_day()
    days = pd.read_csv(tmp_path / "logs" / "days.csv")
    grid = days[days["sleeve"] == "grid"].iloc[0]
    assert grid["value"] == pytest.approx(2500 + bot.grid.slices[0].qty * (597 - 594), abs=0.01)
    assert set(days["sleeve"]) == {"dip", "grid"}
