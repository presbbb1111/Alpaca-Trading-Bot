"""Phase 4: forward-test the 10 AM model on the Alpaca paper account.

    python live_paper.py                       trade today (launchd starts it at 9:25 ET)
    python live_paper.py --dry-run             run today, but log orders instead of sending them
    python live_paper.py --replay 2026-11-13   replay a past day's IEX bars with a fake broker

The same DayModel as the backtest is fed live 1-minute IEX bars from Alpaca's
stream (with the REST API filling any gap). Each signal becomes a market
bracket order: stop-loss at the model's stop, take-profit at 4R.

Guards: paper account PAPER_ACCOUNT_ID only, equity of at least MIN_EQUITY,
at most 5 trades a day, no new trades once the day reaches -3R, one position
at a time, early-close days skipped, everything the bot holds closed at 15:55
ET, and a file named KILL next to this script closes out and stops the bot.
Client order ids are fixed per trade ("tenam-YYYYMMDD-QQQ-1"), so a restart or
reconnect never sends the same order twice. The account is shared with the
other bots: this one only touches its own orders and the shares it bought.

Logs: logs/live.log (everything), logs/trades_live.csv (one row per signal).
"""

import argparse
import logging
import math
import os
import queue
import sys
import threading
import time as _time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config
import data
from strategy import DayModel, Params, r_with_costs

HERE = Path(__file__).resolve().parent
sys.path.append(str(HERE.parent))  # the main bot's Discord alerts (appended: tenam's config stays first)
import notify  # noqa: E402

NY = ZoneInfo("America/New_York")
LOGS = HERE / "logs"
TRADES_CSV = LOGS / "trades_live.csv"
KILL = HERE / "KILL"
PREFIX = "tenam-"
FRESH_SECONDS = 90  # act on a signal only if its bar closed this recently
STALE_SECONDS = 75  # no bar for this long: fetch the missing ones over REST
POLL_SECONDS = 5
OPEN_STATES = {"new", "accepted", "pending_new", "held", "partially_filled", "accepted_for_bidding", "pending_replace"}
DONE_STATES = {"canceled", "expired", "rejected", "done_for_day", "replaced", "stopped", "suspended"}

log = logging.getLogger("tenam")


def setup_logging(name="live.log"):
    LOGS.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(LOGS / name), logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def floor_cent(x):
    return math.floor(x * 100 + 1e-9) / 100


def ceil_cent(x):
    return math.ceil(x * 100 - 1e-9) / 100


# --- brokers ---
class AlpacaBroker:
    """The paper account, through alpaca-py. Orders are looked up by client id."""

    def __init__(self, trading):
        self.t = trading

    def order(self, client_id):
        from alpaca.common.exceptions import APIError
        from alpaca.trading.requests import GetOrderByIdRequest

        try:
            o = self.t.get_order_by_client_id(client_id)
        except APIError as e:
            if "not found" in str(e).lower() or getattr(e, "status_code", None) == 404:
                return None
            raise
        o = self.t.get_order_by_id(o.id, GetOrderByIdRequest(nested=True))
        return self._as_dict(o)

    @staticmethod
    def _as_dict(o):
        def one(x):
            return {"id": str(x.id), "status": x.status.value, "type": x.order_type.value,
                    "filled_qty": float(x.filled_qty or 0),
                    "filled_avg_price": float(x.filled_avg_price) if x.filled_avg_price else None,
                    "filled_at": pd.Timestamp(x.filled_at).tz_convert(NY) if x.filled_at else None}
        d = one(o)
        d["legs"] = [one(leg) for leg in (o.legs or [])]
        return d

    def submit_bracket(self, symbol, side, qty, stop, target, client_id):
        from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

        req = MarketOrderRequest(
            symbol=symbol, qty=qty, side=OrderSide.BUY if side == "long" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY, order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=target), stop_loss=StopLossRequest(stop_price=stop),
            client_order_id=client_id)
        return str(self.t.submit_order(req).id)

    def submit_market(self, symbol, side, qty, client_id):
        """side: "buy" or "sell"."""
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        req = MarketOrderRequest(symbol=symbol, qty=qty, side=OrderSide(side),
                                 time_in_force=TimeInForce.DAY, client_order_id=client_id)
        return str(self.t.submit_order(req).id)

    def cancel(self, order_id):
        from alpaca.common.exceptions import APIError

        try:
            self.t.cancel_order_by_id(order_id)
        except APIError as e:
            log.warning(f"Cancel {order_id}: {e}")

    def position_qty(self, symbol):
        """Signed shares held (short is negative), 0 if none."""
        from alpaca.common.exceptions import APIError

        try:
            p = self.t.get_open_position(symbol)
        except APIError:
            return 0
        qty = abs(float(p.qty))
        return -qty if p.side.value == "short" else qty


class FakeBroker:
    """For --replay and tests: market orders fill at the next bar's open; bracket
    legs fill at their price on later bars, the stop first if both are touched."""

    def __init__(self):
        self.orders = {}  # client id -> order dict
        self.pos = 0
        self.n = 0

    def order(self, client_id):
        o = self.orders.get(client_id)
        return None if o is None else {**o, "legs": [dict(leg) for leg in o["legs"]]}

    def _new(self, client_id, kind, side, qty):
        self.n += 1
        return {"id": f"o{self.n}", "client_id": client_id, "status": "new", "type": kind, "side": side,
                "qty": qty, "filled_qty": 0, "filled_avg_price": None, "filled_at": None, "legs": []}

    def submit_bracket(self, symbol, side, qty, stop, target, client_id):
        if client_id in self.orders:
            raise RuntimeError("client_order_id must be unique")
        o = self._new(client_id, "market", "buy" if side == "long" else "sell", qty)
        exit_side = "sell" if side == "long" else "buy"
        o["legs"] = [{**self._new(client_id + "-tp", "limit", exit_side, qty), "status": "held", "price": target},
                     {**self._new(client_id + "-sl", "stop", exit_side, qty), "status": "held", "price": stop}]
        self.orders[client_id] = o
        return o["id"]

    def submit_market(self, symbol, side, qty, client_id):
        if client_id in self.orders:
            raise RuntimeError("client_order_id must be unique")
        self.orders[client_id] = self._new(client_id, "market", side, qty)
        return self.orders[client_id]["id"]

    def cancel(self, order_id):
        for o in self.orders.values():
            for x in [o] + o["legs"]:
                if x["id"] == order_id and x["status"] in OPEN_STATES:
                    x["status"] = "canceled"

    def position_qty(self, symbol):
        return self.pos

    def _fill(self, x, price, ts):
        x.update(status="filled", filled_qty=x["qty"], filled_avg_price=price, filled_at=ts)
        self.pos += x["qty"] if x["side"] == "buy" else -x["qty"]

    def on_bar(self, bar):
        ts = bar["timestamp"]
        for o in self.orders.values():
            if o["status"] == "new":
                self._fill(o, bar["open"], ts)
                for leg in o["legs"]:
                    leg["status"] = "new"
            legs = {leg["type"]: leg for leg in o["legs"]}
            if o["status"] != "filled" or not legs or legs["stop"]["status"] != "new":
                continue
            stop, tp = legs["stop"], legs["limit"]
            long = stop["side"] == "sell"
            hit_stop = bar["low"] <= stop["price"] if long else bar["high"] >= stop["price"]
            hit_tp = bar["high"] >= tp["price"] if long else bar["low"] <= tp["price"]
            gapped = bar["open"] <= stop["price"] if long else bar["open"] >= stop["price"]
            if hit_stop:
                self._fill(stop, bar["open"] if gapped else stop["price"], ts)
                tp["status"] = "canceled"
            elif hit_tp:
                self._fill(tp, tp["price"], ts)
                stop["status"] = "canceled"


# --- the bot ---
@dataclass
class Signal:
    trade_no: int
    side: str
    signal_time: pd.Timestamp  # the retest bar
    status: str = "pending"  # missed, skipped, dry run, submitted, filled, closed, not filled
    reason: str = ""
    client_id: str = ""
    shares: int = 0
    order_entry_est: float = None
    order_stop: float = None
    order_target: float = None
    order_risk: float = None
    entry_fill_time: pd.Timestamp = None
    entry_fill: float = None
    exit_fill_time: pd.Timestamp = None
    exit_fill: float = None
    exit_kind: str = ""
    flat_client_id: str = ""

    @property
    def sign(self):
        return 1 if self.side == "long" else -1

    @property
    def live_r(self):
        if self.entry_fill is None or self.exit_fill is None:
            return None
        return self.sign * (self.exit_fill - self.entry_fill) / self.order_risk


class LiveBot:
    def __init__(self, day, symbol, broker, median_width, session_close, dry_run=False, alerts=True,
                 csv_path=TRADES_CSV, sleep=_time.sleep):
        self.day, self.symbol, self.broker = day, symbol, broker
        self.dry_run, self.alerts, self.csv_path, self.sleep = dry_run, alerts, csv_path, sleep
        self.model = DayModel(median_width, session_close, Params())
        self.close_dt = datetime.combine(day, session_close, NY)
        self.flat_dt = datetime.combine(day, self.model.flat_time, NY)
        self.signals = []
        self.last_ts = None
        self.halted = ""  # why no new trades are taken (daily loss, KILL)
        self.flattened = False

    # alerts
    def alert(self, title, text="", kind="info"):
        log.info(f"{title}. {text}".strip())
        if self.alerts:
            notify.send(f"10 AM model: {title}", text, kind)

    # bars
    def on_bar(self, bar, now):
        ts = bar["timestamp"]
        if (self.last_ts is not None and ts <= self.last_ts) or ts.time() < config.OR_START or ts >= self.close_dt:
            return
        self.last_ts = ts
        before = self.model.pending_entry
        n_before = len(self.model.result.trades)
        self.model.on_bar(bar)
        if len(self.model.result.trades) > n_before:
            t = self.model.result.trades[-1]
            log.info(f"Model entry #{t.trade_no} {t.side} at {t.entry:.2f} (bar open {ts:%H:%M}), "
                     f"stop {t.stop:.2f}, target {t.target:.2f}")
        if self.model.result.skipped and n_before == 0 and ts.time() == config.OR_END:
            log.info(f"Day skipped by the model: {self.model.result.skipped}")
        pend = self.model.pending_entry
        if pend is not None and pend is not before:
            fresh = (now - (ts + timedelta(minutes=1))).total_seconds() <= FRESH_SECONDS
            self.on_signal(pend, bar, fresh)

    def on_signal(self, pend, bar, fresh):
        side, setup, retest = pend
        n = len(self.model.result.trades) + 1
        sig = Signal(n, side, retest["timestamp"])
        self.signals.append(sig)
        next_bar = (retest["timestamp"] + timedelta(minutes=1)).time()
        log.info(f"Signal #{n}: {side} retest at {retest['timestamp']:%H:%M} (break {setup.break_time:%H:%M}), "
                 f"close {bar['close']:.2f}")
        if not fresh:
            self.skip(sig, "missed: the bot wasn't running when this bar closed", "missed")
        elif next_bar > self.model.p.last_entry:
            self.skip(sig, f"entry would be after {self.model.p.last_entry:%H:%M}")
        elif n > self.model.p.max_trades:
            self.skip(sig, f"already {self.model.p.max_trades} trades today")
        elif self.halted:
            self.skip(sig, self.halted)
        elif self.realized_r() <= -config.MAX_DAILY_LOSS_R:
            self.halted = f"daily loss limit ({self.realized_r():+.2f}R)"
            self.skip(sig, self.halted)
        elif any(s.status in ("submitted", "filled") for s in self.signals[:-1]):
            self.skip(sig, "still in the previous trade")
        else:
            self.place(sig, bar["close"])
        self.write_csv()

    def skip(self, sig, reason, status="skipped"):
        sig.status, sig.reason = status, reason
        log.info(f"Signal #{sig.trade_no} not traded: {reason}")

    def realized_r(self):
        return sum(s.live_r for s in self.signals if s.live_r is not None)

    def place(self, sig, est_entry):
        levels = self.model.levels(est_entry)
        if levels is None:
            return self.skip(sig, "price already past the stop")
        stop, _, target, _ = levels
        if sig.side == "long":
            stop, target = floor_cent(stop), round(target, 2)
            risk = est_entry - stop
        else:
            stop, target = ceil_cent(stop), round(target, 2)
            risk = stop - est_entry
        qty = math.floor(min(config.LIVE_RISK_DOLLARS / risk, config.LIVE_MAX_NOTIONAL / est_entry))
        if qty < 1:
            return self.skip(sig, "position would be under 1 share")
        sig.client_id = f"{PREFIX}{self.day:%Y%m%d}-{self.symbol}-{sig.trade_no}"
        sig.shares, sig.order_entry_est, sig.order_stop, sig.order_target, sig.order_risk = qty, est_entry, stop, target, risk
        if self.dry_run:
            sig.status = "dry run"
            return log.info(f"Dry run: would {sig.side} {qty} {self.symbol} ~{est_entry:.2f}, stop {stop:.2f}, "
                            f"target {target:.2f} ({sig.client_id})")
        sent = self.broker.order(sig.client_id)
        if sent is not None:  # sent before a restart: follow it, never resend
            sig.status = "submitted"
            sig.shares = int(sent["filled_qty"] or qty)
            return log.info(f"{sig.client_id} was already sent before a restart; following it, not resending")
        held = self.broker.position_qty(self.symbol)
        if held:
            sig.client_id = ""
            return self.skip(sig, f"the account already holds {held:g} {self.symbol} (another bot?)")
        self.broker.submit_bracket(self.symbol, sig.side, qty, stop, target, sig.client_id)
        sig.status = "submitted"
        verb = "Buying" if sig.side == "long" else "Shorting"
        self.alert(f"{verb} {qty} {self.symbol}",
                   f"Trade #{sig.trade_no}: {sig.side} after the retest at {sig.signal_time:%H:%M}. "
                   f"Entry ~{est_entry:.2f}, stop {stop:.2f}, target {target:.2f} "
                   f"(${qty * risk:,.0f} at risk).", "buy")

    # orders
    def poll(self, now):
        for sig in self.signals:
            if sig.status not in ("submitted", "filled"):
                continue
            o = self.broker.order(sig.client_id)
            if o is None:
                continue
            if sig.status == "submitted":
                if o["status"] == "filled":
                    sig.status, sig.entry_fill, sig.entry_fill_time = "filled", o["filled_avg_price"], o["filled_at"]
                    log.info(f"Entry filled: #{sig.trade_no} {sig.shares} at {sig.entry_fill:.2f}")
                elif o["status"] in DONE_STATES:
                    sig.status, sig.reason = "not filled", f"entry order {o['status']}"
                    log.warning(f"#{sig.trade_no} entry order {o['status']}")
            if sig.status == "filled":
                exit_leg = next((leg for leg in o["legs"] if leg["status"] == "filled"), None)
                if exit_leg:
                    self.closed(sig, exit_leg, "target" if exit_leg["type"] == "limit" else "stop")
                elif sig.flat_client_id:
                    f = self.broker.order(sig.flat_client_id)
                    if f and f["status"] == "filled":
                        self.closed(sig, f, "time")
        self.write_csv()

    def closed(self, sig, fill, kind):
        sig.status, sig.exit_kind = "closed", kind
        sig.exit_fill, sig.exit_fill_time = fill["filled_avg_price"], fill["filled_at"]
        r = sig.live_r
        pnl = sig.sign * (sig.exit_fill - sig.entry_fill) * sig.shares
        self.alert(f"Trade #{sig.trade_no} closed at the {kind}: {r:+.2f}R",
                   f"{sig.side} {sig.shares} {self.symbol}: in {sig.entry_fill:.2f}, out {sig.exit_fill:.2f}, "
                   f"${pnl:+,.2f}. Day so far {self.realized_r():+.2f}R.", "profit" if pnl >= 0 else "loss")
        if self.realized_r() <= -config.MAX_DAILY_LOSS_R and not self.halted:
            self.halted = f"daily loss limit ({self.realized_r():+.2f}R)"
            self.alert("No more trades today", f"The day reached {self.realized_r():+.2f}R.", "warning")

    def flatten(self, why):
        """Cancel this bot's open orders and close the shares it holds."""
        self.flattened = True
        for sig in self.signals:
            if sig.status not in ("submitted", "filled"):
                continue
            o = self.broker.order(sig.client_id)
            if o is None:
                continue
            if sig.status == "submitted" and o["status"] != "filled":
                self.broker.cancel(o["id"])
                if not o["filled_qty"]:
                    sig.status, sig.reason = "not filled", f"cancelled: {why}"
                    continue
                sig.shares = int(o["filled_qty"])  # partly filled: close what we got
            for leg in o["legs"]:
                if leg["status"] in OPEN_STATES:
                    self.broker.cancel(leg["id"])
            for _ in range(20):  # wait for the cancels before selling, so shares aren't held by them
                o = self.broker.order(sig.client_id)
                if not any(leg["status"] in OPEN_STATES for leg in o["legs"]):
                    break
                self.sleep(0.5)
            if sig.status == "submitted":
                sig.status, sig.entry_fill, sig.entry_fill_time = "filled", o["filled_avg_price"], o["filled_at"]
            exit_leg = next((leg for leg in o["legs"] if leg["status"] == "filled"), None)
            if exit_leg:  # it hit the stop or target while we were cancelling
                self.closed(sig, exit_leg, "target" if exit_leg["type"] == "limit" else "stop")
                continue
            held = self.broker.position_qty(self.symbol)
            if held * sig.sign < sig.shares:
                log.warning(f"#{sig.trade_no}: expected {sig.sign * sig.shares} {self.symbol}, account has {held:g}; "
                            "not sending a close order")
                continue
            sig.flat_client_id = f"{sig.client_id}-flat"
            if self.broker.order(sig.flat_client_id) is None:
                self.broker.submit_market(self.symbol, "sell" if sig.side == "long" else "buy", sig.shares,
                                          sig.flat_client_id)
                log.info(f"Closing #{sig.trade_no}: {sig.shares} {self.symbol} at market ({why})")
        self.write_csv()

    def settled(self):
        return all(s.status not in ("submitted", "filled") for s in self.signals)

    # records
    def rows(self):
        trades = {t.trade_no: t for t in self.model.result.trades}
        out = []
        for s in self.signals:
            t = trades.get(s.trade_no)
            same = t is not None and t.side == s.side and t.retest_time == s.signal_time
            row = {"date": self.day.isoformat(), "symbol": self.symbol, "trade_no": s.trade_no, "side": s.side,
                   "signal_time": s.signal_time, "status": s.status, "reason": s.reason}
            if same:
                row.update(intended_entry_time=t.entry_time, intended_entry=t.entry, intended_stop=t.stop,
                           intended_target=t.target, intended_exit_time=t.exit_time, intended_exit=t.exit_price,
                           intended_exit_kind=t.exit_kind,
                           intended_r=r_with_costs(t) if t.exit_price is not None else None)
            d = asdict(s)
            for k in ("client_id", "shares", "order_entry_est", "order_stop", "order_target", "order_risk",
                      "entry_fill_time", "entry_fill", "exit_fill_time", "exit_fill", "exit_kind"):
                row[k] = d[k]
            row["live_r"] = s.live_r
            if same and s.entry_fill is not None:
                row["entry_slippage"] = s.sign * (s.entry_fill - t.entry)  # positive = worse than intended
            if s.exit_fill is not None:
                planned = {"stop": s.order_stop, "target": s.order_target}.get(s.exit_kind)
                if planned is not None:
                    row["exit_slippage"] = s.sign * (planned - s.exit_fill)
            out.append(row)
        return out

    def write_csv(self):
        new = pd.DataFrame(self.rows())
        if self.csv_path.exists():
            old = pd.read_csv(self.csv_path)
            old = old[old["date"] != self.day.isoformat()]
            new = pd.concat([old, new], ignore_index=True) if len(new) else old
        if len(new):
            self.csv_path.parent.mkdir(exist_ok=True)
            new.to_csv(self.csv_path, index=False)

    def write_day(self, note=""):
        """One row per day the bot ran, signals or not (weekly_report.py needs the quiet days too)."""
        path = self.csv_path.with_name(self.csv_path.name.replace("trades_", "days_"))
        res = self.model.result
        traded = [s.live_r for s in self.signals if s.live_r is not None]
        row = pd.DataFrame([{"date": self.day.isoformat(), "or_high": res.or_high, "or_low": res.or_low,
                             "median_width": res.median_width, "model_skipped": res.skipped or "",
                             "signals": len(self.signals), "live_trades": len(traded),
                             "live_r": sum(traded), "note": note}])
        if path.exists():
            old = pd.read_csv(path)
            row = pd.concat([old[old["date"] != self.day.isoformat()], row], ignore_index=True)
        row.to_csv(path, index=False)

    def summary(self):
        res = self.model.result
        traded = [s for s in self.signals if s.live_r is not None]
        lines = [f"Range {res.or_low:.2f}-{res.or_high:.2f}" if res.or_high is not None else "No range"]
        if res.skipped:
            lines.append(f"Model skipped the day: {res.skipped}")
        for s in self.signals:
            r = f"{s.live_r:+.2f}R ({s.exit_kind})" if s.live_r is not None else f"{s.status}: {s.reason}"
            lines.append(f"#{s.trade_no} {s.side} at {s.signal_time:%H:%M}: {r}")
        model_r = sum(r_with_costs(t) for t in res.trades if t.exit_price is not None)
        lines.append(f"Live {sum(s.live_r for s in traded):+.2f}R on {len(traded)} trades; "
                     f"the model on IEX bars says {model_r:+.2f}R on {len(res.trades)}.")
        return "\n".join(lines)


# --- running ---
def keys():
    return data.keys()


def paper_guard(trading):
    base = getattr(trading, "_base_url", "")
    base = str(getattr(base, "value", base))
    if "paper-api.alpaca.markets" not in base:
        raise SystemExit(f"Refusing to run: not the paper API ({base or 'unknown URL'}).")
    acct = trading.get_account()
    if acct.account_number != config.PAPER_ACCOUNT_ID:
        raise SystemExit(f"Refusing to run: account {acct.account_number} is not {config.PAPER_ACCOUNT_ID}.")
    if float(acct.equity) < config.MIN_EQUITY:
        raise SystemExit(f"Refusing to run: equity ${float(acct.equity):,.0f} is under ${config.MIN_EQUITY:,}.")
    return acct


def median_width_before(day, symbol, refresh=True):
    """Median opening-range width of the 20 sessions before `day`, from SIP history."""
    if refresh:
        cal = data.download_calendar(day)
        data.download(symbol, cal, "sip")
    bars = data.load_bars(symbol)
    or_bars = bars[(bars["date"] < pd.Timestamp(day)) & (bars["timestamp"].dt.time < config.OR_END)]
    w = or_bars.groupby("date").agg(h=("high", "max"), l=("low", "min"))
    w = (w["h"] - w["l"]).tail(config.OR_MEDIAN_DAYS)
    if len(w) < config.OR_MEDIAN_DAYS:
        return None
    log.info(f"20-day median range {w.median():.2f} (last cached day {w.index[-1]:%Y-%m-%d})")
    return float(w.median())


def rest_bars(hist, symbol, start, end):
    from alpaca.data.enums import DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    df = hist.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                                              start=start, end=end, feed=DataFeed(config.LIVE_FEED))).df
    if df.empty:
        return []
    df = df.reset_index()
    df["timestamp"] = df["timestamp"].dt.tz_convert(NY)
    return df[["timestamp", "open", "high", "low", "close"]].to_dict("records")


def start_stream(symbol, q):
    from alpaca.data.enums import DataFeed
    from alpaca.data.live import StockDataStream

    stream = StockDataStream(*keys(), feed=DataFeed(config.LIVE_FEED))

    async def on_bar(b):
        q.put({"timestamp": pd.Timestamp(b.timestamp).tz_convert(NY), "open": b.open, "high": b.high,
               "low": b.low, "close": b.close})

    stream.subscribe_bars(on_bar, symbol)

    def run():
        try:
            stream.run()
        except Exception as e:  # the REST fallback keeps the bot going
            log.warning(f"Bar stream stopped: {e}")

    threading.Thread(target=run, daemon=True).start()
    return stream


def run_live(dry_run):
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    if os.getenv("ALPACA_PAPER", "true").lower() != "true":
        raise SystemExit("Refusing to run: ALPACA_PAPER isn't true.")
    trading = TradingClient(*keys(), paper=True)
    acct = paper_guard(trading)
    today = datetime.now(NY).date()
    log.info(f"Starting for {today} on paper account {acct.account_number}{' (dry run)' if dry_run else ''}")
    days = trading.get_calendar(GetCalendarRequest(start=today, end=today))
    if not days or days[0].date != today:
        return log.info("Market closed today.")
    session_close = days[0].close.time()
    if session_close != datetime.strptime("16:00", "%H:%M").time():
        return log.info(f"Early close at {session_close:%H:%M}: skipping today.")
    if KILL.exists():
        return log.info(f"{KILL} exists: not trading. Delete it to start again.")
    symbol = config.LIVE_SYMBOL
    median = median_width_before(today, symbol)
    if median is None:
        return log.error("Not enough history for the 20-day median range.")

    broker = AlpacaBroker(trading)
    bot = LiveBot(today, symbol, broker, median, session_close, dry_run=dry_run)
    open_dt = datetime.combine(today, config.OR_START, NY)
    clock = trading.get_clock()
    if not clock.is_open and datetime.now(NY) < open_dt:
        wait = (open_dt - datetime.now(NY)).total_seconds()
        log.info(f"Waiting {wait / 60:.0f} minutes for the open")
        _time.sleep(max(wait, 0))

    hist = StockHistoricalDataClient(*keys())
    q = queue.Queue()
    stream = start_stream(symbol, q)

    def catch_up(now):
        start = (bot.last_ts + timedelta(minutes=1)) if bot.last_ts is not None else open_dt
        end = now.replace(second=0, microsecond=0)  # bars that have closed
        if end > start:
            for bar in rest_bars(hist, symbol, start, end):
                if bar["timestamp"] + timedelta(minutes=1) <= now:  # closed bars only
                    bot.on_bar(bar, now)

    catch_up(datetime.now(NY))  # a late start or restart rebuilds the day first
    last_poll = 0.0
    while True:
        now = datetime.now(NY)
        try:
            if KILL.exists() and not bot.halted.startswith("KILL"):
                bot.halted = "KILL file"
                bot.alert("KILL file found", "Closing this bot's trades and stopping for the day.", "warning")
                bot.flatten("KILL file")
            while not q.empty():
                bar = q.get_nowait()
                if bot.last_ts is not None and bar["timestamp"] > bot.last_ts + timedelta(minutes=1):
                    catch_up(now)  # fill a gap from a dropped connection first
                bot.on_bar(bar, now)
            last_end = (bot.last_ts + timedelta(minutes=1)) if bot.last_ts is not None else open_dt
            if (now - last_end).total_seconds() > STALE_SECONDS and now < bot.close_dt:
                catch_up(now)
            if _time.monotonic() - last_poll >= POLL_SECONDS:
                bot.poll(now)
                last_poll = _time.monotonic()
            if now >= bot.flat_dt and not bot.flattened:
                bot.flatten("15:55 close-out")
            # Stay until the 15:55 bar has closed, so the model's own exit is on record too.
            done = bot.settled() and now >= bot.flat_dt + timedelta(seconds=75)
            if bot.flattened and (done or now >= bot.flat_dt + timedelta(minutes=3)):
                break
        except Exception as e:
            log.exception("Loop error")
            bot.alert("Error", str(e)[:500], "error")
            _time.sleep(10)
        _time.sleep(1)
    try:
        stream.stop()
    except Exception:
        pass
    catch_up(datetime.now(NY))  # the model's own 15:55 exit, for the record
    bot.poll(datetime.now(NY))
    bot.write_csv()
    bot.write_day(bot.halted)
    bot.alert(f"Day done ({today})", bot.summary(), "info")


def run_replay(day, alerts=False):
    """Feed a past day's IEX bars through the bot with a fake broker."""
    from alpaca.data.historical import StockHistoricalDataClient

    symbol = config.LIVE_SYMBOL
    cal = data.load_calendar().set_index("date")
    session_close = cal.loc[pd.Timestamp(day), "close"].time()
    median = median_width_before(day, symbol, refresh=False)
    start = datetime.combine(day, config.OR_START, NY)
    bars = rest_bars(StockHistoricalDataClient(*keys()), symbol, start, start + timedelta(hours=7))
    broker = FakeBroker()
    bot = LiveBot(day, symbol, broker, median, session_close, alerts=alerts,
                  csv_path=LOGS / "trades_replay.csv", sleep=lambda s: None)
    return replay(bot, broker, bars)


def replay(bot, broker, bars):
    for bar in bars:
        now = bar["timestamp"] + timedelta(minutes=1, seconds=2)
        broker.on_bar(bar)  # orders sent after the last bar fill at this bar's open
        bot.poll(now)
        bot.on_bar(bar, now)
        if now >= bot.flat_dt and not bot.flattened:
            bot.flatten("15:55 close-out")
    bot.poll(bars[-1]["timestamp"] + timedelta(minutes=2))
    bot.write_csv()
    bot.write_day("replay")
    return bot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replay", metavar="YYYY-MM-DD")
    args = parser.parse_args()
    if args.replay:
        setup_logging("replay.log")
        bot = run_replay(date.fromisoformat(args.replay))
        print(bot.summary())
        return
    setup_logging()
    try:
        run_live(args.dry_run)
    except SystemExit as e:
        log.error(str(e))
        notify.send("10 AM model: not running", str(e), "error")
        raise


if __name__ == "__main__":
    main()
