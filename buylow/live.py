"""Paper-trade the two approved "buy low, sell high" sleeves, $2,500 each.

    python live.py              the scheduled run: from 9:25 ET to the close
    python live.py --dry-run    the same with live prices, but orders are only logged
    python live.py --status     what each sleeve holds now

Dip buying: at the open it sells what bounced (or was held 10 days) and buys
the most oversold uptrend dips from yesterday's close, a third of the sleeve
each, with market orders.

Ladder: five limit buy orders on VOO, 1% apart under the anchor. Each slice
that fills gets a limit sell 1% above its own buy price; each slice that
sells gets its buy order back. While nothing is held the ladder follows the
price up. The orders are day orders, so they're placed again every morning.

Both use the rules in models.py, the same code the backtest ran. Only orders
whose client ids start with "bl-" are this bot's: it never touches the other
bots' orders or shares, and it skips symbols they hold. Create a file named
KILL in this folder to cancel its orders and stop; what it holds stays.
"""

import argparse
import json
import logging
import math
import sys
import time
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

import config
import data
import models

HERE = Path(__file__).resolve().parent
LOGS = HERE / "logs"
KILL = HERE / "KILL"
ET = ZoneInfo("America/New_York")
PREFIX = "bl-"
DONE = {"filled", "canceled", "expired", "rejected"}  # no more fills will come

sys.path.append(str(HERE.parent))
import notify  # noqa: E402

log = logging.getLogger("buylow")


def setup_logging(name):
    LOGS.mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    for h in (logging.FileHandler(LOGS / name), logging.StreamHandler()):
        h.setFormatter(fmt)
        log.addHandler(h)
    log.setLevel(logging.INFO)


def floor_to(x, places):
    f = 10 ** places
    return math.floor(x * f) / f


# --- Broker ------------------------------------------------------------------

class AlpacaBroker:
    """The paper account. Prices and bars come from the free IEX feed."""

    def __init__(self):
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        key, secret = data.keys()
        self.trading = TradingClient(key, secret, paper=True)
        self.market = StockHistoricalDataClient(key, secret)

    def submit(self, symbol, side, client_id, qty=None, notional=None, limit=None):
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

        kw = {"symbol": symbol, "side": OrderSide(side), "time_in_force": TimeInForce.DAY, "client_order_id": client_id}
        if limit is not None:
            req = LimitOrderRequest(qty=qty, limit_price=limit, **kw)
        elif notional is not None:
            req = MarketOrderRequest(notional=notional, **kw)
        else:
            req = MarketOrderRequest(qty=qty, **kw)
        self.trading.submit_order(req)

    def order(self, client_id):
        """{status, filled_qty, price} or None if Alpaca has no such order."""
        from alpaca.common.exceptions import APIError

        try:
            o = self.trading.get_order_by_client_id(client_id)
        except APIError as e:
            if getattr(e, "status_code", None) == 404 or "not found" in str(e).lower():
                return None
            raise
        return {"status": o.status.value, "filled_qty": float(o.filled_qty or 0),
                "price": float(o.filled_avg_price or 0)}

    def cancel(self, client_id):
        try:
            self.trading.cancel_order_by_id(self.trading.get_order_by_client_id(client_id).id)
        except Exception as e:  # already filled or gone
            log.info(f"Cancel {client_id}: {str(e)[:100]}")

    def positions(self):
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}

    def others_orders(self):
        """Symbols with working orders from the other bots."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = self.trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
        return {o.symbol for o in orders if not (o.client_order_id or "").startswith(PREFIX)}

    def prices(self, symbols):
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        if not symbols:
            return {}
        got = self.market.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
        return {s: float(t.price) for s, t in got.items()}

    def highs_since(self, symbol, since):
        """(timestamp, high) of the 1-minute bars after `since`."""
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame

        df = self.market.get_stock_bars(StockBarsRequest(symbol_or_symbols=symbol, timeframe=TimeFrame.Minute,
                                                         start=since, feed=DataFeed.IEX)).df
        if df.empty:
            return []
        df = df.reset_index()
        return [(t.tz_convert(ET), float(h)) for t, h in zip(df["timestamp"], df["high"]) if t > since]

    def daily_rows(self, symbols, last_close):
        """Each symbol's dip_indicators row for the session that ended `last_close`."""
        df = data.fetch(symbols, datetime.combine(last_close - timedelta(days=420), datetime.min.time()),
                        last_close, None, "split")
        rows = {}
        for s, g in df.groupby("symbol"):
            g = g.assign(date=g["timestamp"].dt.tz_localize(None).dt.normalize())
            ind = models.dip_indicators(g[["date", "open", "high", "low", "close", "volume"]])
            last = ind.iloc[-1]
            if last["date"].date() == last_close:
                rows[s] = last.to_dict()
        return rows


class DryBroker(AlpacaBroker):
    """Live prices, no orders: everything submitted just sits as 'new'."""

    def __init__(self):
        super().__init__()
        self.book = {}

    def submit(self, symbol, side, client_id, qty=None, notional=None, limit=None):
        self.book[client_id] = {"status": "new", "filled_qty": 0.0, "price": 0.0}
        log.info(f"[dry run] {side} {symbol} " + (f"{qty:g} @ ${limit:,.2f} limit" if limit else
                                                    f"${notional:,.2f}" if notional else f"{qty:g} at the market"))

    def order(self, client_id):
        return self.book.get(client_id)

    def cancel(self, client_id):
        if client_id in self.book:
            self.book[client_id]["status"] = "canceled"


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


# --- The bot -----------------------------------------------------------------

def grid_to_dict(g):
    return {"anchor": g.anchor, "cash": g.cash, "slice_dollars": g.slice_dollars,
            "high_since_empty": g.high_since_empty,
            "slices": [{**asdict(s), "bought_at": str(s.bought_at) if s.bought_at else None} for s in g.slices]}


def grid_from_dict(d):
    g = models.Grid(anchor=d["anchor"], cash=d["cash"], slice_dollars=d["slice_dollars"])
    g.high_since_empty = d["high_since_empty"]
    g.slices = [models.Slice(**s) for s in d["slices"]]
    return g


class Bot:
    def __init__(self, broker, day, state_path, calendar=None, alerts=True, sleep=time.sleep,
                 now=lambda: datetime.now(ET)):
        self.broker, self.day, self.path = broker, day, state_path
        self.cal = calendar
        self.alerts, self.sleep, self.now = alerts, sleep, now
        if state_path.exists():
            self.state = json.loads(state_path.read_text())
        else:
            self.state = {"seq": 0, "orders": {}, "values": {},
                          "dip": {"cash": config.LIVE_SLEEVES["dip"], "held": {}, "done": None}, "grid": None}
        self.grid = grid_from_dict(self.state["grid"]) if self.state["grid"] else None

    # bookkeeping
    def save(self):
        if self.grid:
            self.state["grid"] = grid_to_dict(self.grid)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2, default=str))
        tmp.replace(self.path)

    def alert(self, title, text="", kind="info"):
        log.info(f"{title}. {text}".strip())
        if self.alerts:
            notify.send(f"[Buy low] {title}", text, kind)

    def journal(self, text):
        LOGS.mkdir(exist_ok=True)
        with open(LOGS / "journal.txt", "a") as f:
            f.write(f"{self.now():%Y-%m-%d %I:%M %p} ET  {text}\n")

    def working(self, sleeve=None, side=None, slice_n=None):
        return {cid: m for cid, m in self.state["orders"].items()
                if (sleeve is None or m["sleeve"] == sleeve) and (side is None or m["side"] == side)
                and (slice_n is None or m.get("slice") == slice_n)}

    def place(self, sleeve, symbol, side, qty=None, notional=None, limit=None, slice_n=None):
        self.state["seq"] += 1
        cid = f"{PREFIX}{sleeve}-{self.day:%Y%m%d}-{self.state['seq']}"
        self.state["orders"][cid] = {"sleeve": sleeve, "symbol": symbol, "side": side, "qty": qty,
                                     "notional": notional, "limit": limit, "slice": slice_n, "day": str(self.day)}
        self.save()  # saved first, so a restart looks the order up instead of sending another
        try:
            self.broker.submit(symbol, side, cid, qty=qty, notional=notional, limit=limit)
        except Exception as e:
            del self.state["orders"][cid]
            self.save()
            self.alert(f"Order refused: {side} {symbol}", str(e)[:300], "error")
            return None
        return cid

    # fills
    def reconcile(self):
        """Apply every finished order of ours to the sleeves."""
        for cid, m in list(self.state["orders"].items()):
            o = self.broker.order(cid)
            if o is None:  # never reached Alpaca (crash between saving and sending)
                del self.state["orders"][cid]
                continue
            if o["status"] not in DONE:
                continue
            del self.state["orders"][cid]
            if o["filled_qty"] > 0:
                (self.dip_fill if m["sleeve"] == "dip" else self.grid_fill)(m, o["filled_qty"], o["price"])
            elif o["status"] == "rejected":
                self.alert(f"Order rejected: {m['side']} {m['symbol']}", "", "error")
        self.save()

    def dip_fill(self, m, qty, price):
        d = self.state["dip"]
        s = m["symbol"]
        if m["side"] == "buy":
            h = d["held"].get(s)
            if h:  # a second fill on the same symbol: average it in
                h["entry"] = (h["entry"] * h["qty"] + price * qty) / (h["qty"] + qty)
                h["qty"] += qty
            else:
                d["held"][s] = {"qty": qty, "entry": price, "entry_date": m["day"]}
            d["cash"] -= qty * price
            self.alert(f"Dip: bought {s}", f"{qty:.4f} shares at ${price:,.2f} (${qty * price:,.2f})", "buy")
            self.journal(f"DIP BUY {s} {qty:.4f} @ ${price:,.2f}")
        else:
            h = d["held"][s]
            sold = min(qty, h["qty"])
            pl = sold * (price - h["entry"])
            d["cash"] += sold * price
            h["qty"] -= sold
            if h["qty"] < 1e-6:
                del d["held"][s]
            self.alert(f"Dip: sold {s}", f"{sold:.4f} shares at ${price:,.2f}, bought at ${h['entry']:,.2f}: "
                       f"{'+' if pl >= 0 else '−'}${abs(pl):,.2f}", "profit" if pl >= 0 else "loss")
            self.journal(f"DIP SELL {s} {sold:.4f} @ ${price:,.2f} (in ${h['entry']:,.2f})  P/L {pl:+.2f}")

    def grid_fill(self, m, qty, price):
        g = self.grid
        s = g.slices[m["slice"] - 1]
        if m["side"] == "buy":
            s.buy_price = (s.buy_price * s.qty + price * qty) / (s.qty + qty)
            s.qty += qty
            s.bought_at = str(self.now())
            g.cash -= qty * price
            self.alert(f"Ladder: bought slice {s.n}", f"{qty:.4f} VOO at ${price:,.2f}; selling it at "
                       f"${g.sell_level(s):,.2f}", "buy")
            self.journal(f"GRID BUY slice {s.n} {qty:.4f} @ ${price:,.2f}")
        else:
            sold = min(qty, s.qty)
            pl = sold * (price - s.buy_price)
            g.cash += sold * price
            s.qty -= sold
            result = f"+${pl:,.2f}" if pl >= 0 else f"−${-pl:,.2f}"
            self.alert(f"Ladder: sold slice {s.n}", f"{sold:.4f} VOO at ${price:,.2f}, bought at ${s.buy_price:,.2f}: "
                       f"{result}", "profit" if pl >= 0 else "loss")
            self.journal(f"GRID SELL slice {s.n} {sold:.4f} @ ${price:,.2f} (in ${s.buy_price:,.2f})  P/L {pl:+.2f}")
            if s.qty < 1e-6:
                s.qty, s.buy_price, s.bought_at = 0.0, 0.0, None

    def wait_for(self, sleeve, seconds):
        deadline = self.now() + timedelta(seconds=seconds)
        while True:
            self.reconcile()
            if not self.working(sleeve) or self.now() >= deadline:
                return
            self.sleep(3)

    # dip buying
    def last_close(self):
        days = self.cal[self.cal["date"] < pd.Timestamp(self.day)]["date"]
        return days.iloc[-1].date()

    def days_held(self, entry_date, last_close):
        d = self.cal["date"]
        return int(((d >= pd.Timestamp(entry_date)) & (d <= pd.Timestamp(last_close))).sum())

    def busy(self):
        """Symbols the other bots hold or have orders in."""
        mine = set(self.state["dip"]["held"]) | {config.GRID_SYMBOL}
        held = {s for s, q in self.broker.positions().items() if q} - mine
        return held | self.broker.others_orders() | set(config.LIVE_EXCLUDE)

    def dip_open(self):
        d = self.state["dip"]
        if d["done"] == str(self.day):
            return
        last = self.last_close()
        rows = self.broker.daily_rows(config.DIP_UNIVERSE, last)
        if len(rows) < len(config.DIP_UNIVERSE) * 0.8:
            self.alert("Dip: skipped today", f"Only {len(rows)} of {len(config.DIP_UNIVERSE)} symbols had a "
                       f"{last} close.", "warning")
            d["done"] = str(self.day)
            self.save()
            return
        held_days = {s: self.days_held(h["entry_date"], last) for s, h in d["held"].items()}
        busy = self.busy()
        skipped = sorted(s for s, r in rows.items() if r["buy"] and s in busy and s not in d["held"])
        if skipped:
            log.info(f"Dip: leaving out {', '.join(skipped)}: another bot holds them or has orders in them")
        sells, buys = models.dip_decide({s: r for s, r in rows.items() if s in d["held"] or s not in busy}, held_days)
        positions = self.broker.positions()
        for s in sells:
            qty = min(d["held"][s]["qty"], positions.get(s, 0.0))
            why = "bounced above its 5-day average" if rows[s]["bounced"] else f"held {held_days[s]} days"
            log.info(f"Dip: selling {s} ({why})")
            if qty > 0:
                self.place("dip", s, "sell", qty=floor_to(qty, 9))
        self.wait_for("dip", config.FILL_WAIT_SECONDS)
        value = d["cash"] + sum(h["qty"] * rows[s]["close"] for s, h in d["held"].items() if s in rows)
        for s in buys:
            if len(d["held"]) + len(self.working("dip", "buy")) >= config.DIP_SLOTS:
                break
            committed = sum(m["notional"] for m in self.working("dip", "buy").values())
            dollars = floor_to(min(value / config.DIP_SLOTS, d["cash"] - committed), 2)
            if dollars < 1:
                break
            log.info(f"Dip: buying ${dollars:,.2f} of {s} (2-day RSI {rows[s]['rsi']:.1f}, close "
                     f"${rows[s]['close']:,.2f}, 200-day average ${rows[s]['trend']:,.2f})")
            self.place("dip", s, "buy", notional=dollars)
        self.wait_for("dip", config.FILL_WAIT_SECONDS)
        if not sells and not buys:
            log.info("Dip: nothing to do today")
        d["done"] = str(self.day)
        self.save()

    # ladder
    def grid_start(self):
        if self.grid is None:
            price = self.broker.prices([config.GRID_SYMBOL])[config.GRID_SYMBOL]
            budget = config.LIVE_SLEEVES["grid"]
            self.grid = models.Grid(anchor=price, cash=budget, slice_dollars=budget / config.GRID_SLICES)
            self.state["grid_bar"] = str(pd.Timestamp(self.now()).floor("min"))  # follow the price from here on
            self.alert("Ladder started", f"VOO at ${price:,.2f}; buying a ${budget / config.GRID_SLICES:,.0f} slice at "
                       + ", ".join(f"${p:,.2f}" for _, _, p in self.grid.orders()))
        self.state.setdefault("grid_bar", None)
        self.save()
        self.grid_orders()

    def grid_orders(self):
        """Place the ladder's missing orders: a sell for each held slice, a buy
        for each empty one while cash allows."""
        g = self.grid
        for side, s, price in g.orders():
            if self.working("grid", slice_n=s.n):
                continue
            if side == "sell":
                self.place("grid", config.GRID_SYMBOL, "sell", qty=floor_to(s.qty, 9), limit=price, slice_n=s.n)
            else:
                committed = sum(m["qty"] * m["limit"] for m in self.working("grid", "buy").values())
                if g.cash - committed < g.slice_dollars - 0.01:
                    continue
                self.place("grid", config.GRID_SYMBOL, "buy", qty=floor_to(g.slice_dollars / price, 6), limit=price,
                           slice_n=s.n)

    def grid_poll(self):
        self.reconcile()
        since = pd.Timestamp(self.state.get("grid_bar") or datetime.combine(self.day, datetime.min.time())
                             .replace(hour=9, minute=29, tzinfo=ET))
        moved = False
        for ts, high in self.broker.highs_since(config.GRID_SYMBOL, since):
            moved |= self.grid.reanchor(high)
            self.state["grid_bar"] = str(ts)
        if moved:
            self.alert("Ladder moved up", f"Anchor ${self.grid.anchor:,.2f}; buys now at "
                       + ", ".join(f"${p:,.2f}" for side, _, p in self.grid.orders() if side == "buy"))
            for cid in self.working("grid", "buy"):
                self.broker.cancel(cid)
            self.reconcile()  # the cancelled ones come back here and are re-placed below
        self.grid_orders()
        self.save()

    # end of day
    def stop(self, why):
        for cid in self.state["orders"]:
            self.broker.cancel(cid)
        self.sleep(3)
        self.reconcile()
        self.alert("Stopped", f"{why}. Orders cancelled; holdings kept.", "warning")

    def values(self, prices):
        d = self.state["dip"]
        dip = d["cash"] + sum(h["qty"] * prices.get(s, h["entry"]) for s, h in d["held"].items())
        grid = self.grid.value(prices.get(config.GRID_SYMBOL, self.grid.anchor)) if self.grid else config.LIVE_SLEEVES["grid"]
        return {"dip": dip, "grid": grid}

    def end_of_day(self):
        self.reconcile()
        symbols = list(self.state["dip"]["held"]) + [config.GRID_SYMBOL]
        values = self.values(self.broker.prices(symbols))
        rows, lines = [], []
        for sleeve, v in values.items():
            before = self.state["values"].get(sleeve, config.LIVE_SLEEVES[sleeve])
            rows.append({"date": str(self.day), "sleeve": sleeve, "value": round(v, 2), "pl": round(v - before, 2)})
            lines.append(f"{'Dip buying' if sleeve == 'dip' else 'Ladder'}: ${v:,.2f} ({v - before:+,.2f} today, "
                         f"{v - config.LIVE_SLEEVES[sleeve]:+,.2f} since the start)")
        path = LOGS / "days.csv"
        old = pd.read_csv(path) if path.exists() else pd.DataFrame(columns=["date", "sleeve", "value", "pl"])
        old = old[old["date"].astype(str) != str(self.day)]
        pd.concat([old, pd.DataFrame(rows)], ignore_index=True).to_csv(path, index=False)
        self.state["values"] = {k: round(v, 2) for k, v in values.items()}
        self.save()
        held = ", ".join(f"{s} {h['qty']:.3f}" for s, h in self.state["dip"]["held"].items()) or "nothing"
        slices = sum(s.held for s in self.grid.slices) if self.grid else 0
        self.alert("Day summary", "\n".join(lines) + f"\nDip holds: {held}. Ladder: {slices} of "
                   f"{config.GRID_SLICES} slices held.")
        self.journal("Day end. " + " | ".join(lines))

    def status(self):
        d = self.state["dip"]
        out = [f"Dip buying: ${d['cash']:,.2f} cash"]
        out += [f"  {s}: {h['qty']:.4f} at ${h['entry']:,.2f} since {h['entry_date']}" for s, h in d["held"].items()]
        if self.grid:
            out.append(f"Ladder: ${self.grid.cash:,.2f} cash, anchor ${self.grid.anchor:,.2f}")
            out += [f"  slice {s.n}: {s.qty:.4f} at ${s.buy_price:,.2f}" if s.held else
                    f"  slice {s.n}: buy at ${self.grid.buy_level(s):,.2f}" for s in self.grid.slices]
        out.append(f"Working orders: {len(self.state['orders'])}")
        return "\n".join(out)


# --- Running it --------------------------------------------------------------

def wait_until(bot, moment):
    while bot.now() < moment:
        if KILL.exists():
            return False
        bot.sleep(min(30, max(1, (moment - bot.now()).total_seconds())))
    return True


def run(dry_run):
    setup_logging("dryrun.log" if dry_run else "live.log")
    broker = DryBroker() if dry_run else AlpacaBroker()
    acct = paper_guard(broker.trading)
    day = datetime.now(ET).date()
    log.info(f"Starting for {day}{' (dry run)' if dry_run else ''}, account {acct.account_number}")
    cal = data.calendar(refresh=True)
    today = cal[cal["date"] == pd.Timestamp(day)]
    if today.empty:
        log.info("Market closed today.")
        return
    open_t, close_t = today["open"].iloc[0].to_pydatetime(), today["close"].iloc[0].to_pydatetime()
    bot = Bot(broker, day, HERE / ("state_dryrun.json" if dry_run else "state.json"), cal, alerts=not dry_run)
    if KILL.exists():
        log.info("KILL file present: not trading today.")
        return
    bot.reconcile()  # anything left from yesterday
    if not wait_until(bot, open_t + timedelta(seconds=15)):
        return bot.stop("KILL file")
    bot.dip_open()
    bot.grid_start()
    errors = 0
    while bot.now() < close_t:
        if KILL.exists():
            return bot.stop("KILL file")
        try:
            bot.grid_poll()
            errors = 0
        except Exception as e:
            errors += 1
            log.exception("poll failed")
            if errors == 5:
                bot.alert("Having trouble", f"5 failed checks in a row: {str(e)[:200]}", "error")
        bot.sleep(config.POLL_SECONDS)
    wait_until(bot, close_t + timedelta(seconds=90))
    bot.end_of_day()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.status:
        path = HERE / ("state_dryrun.json" if args.dry_run else "state.json")
        print(Bot(None, date.today(), path).status() if path.exists() else "Nothing yet: it hasn't run.")
        return
    try:
        run(args.dry_run)
    except SystemExit:
        raise
    except Exception as e:
        log.exception("crashed")
        if not args.dry_run:
            notify.send("[Buy low] Crashed", str(e)[:500], "error")
        raise


if __name__ == "__main__":
    main()
