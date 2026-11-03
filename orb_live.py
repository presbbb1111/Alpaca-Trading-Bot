"""Live paper-trading experiment: Opening Range Breakout on stocks in play.

Same rules as orb_stocks_in_play.py (the backtest), run for real on the paper
account so we can see actual fills:

  9:15 ET  Build today's list: stocks with 14-day average volume >= 1M shares
           and ATR > $0.50, and their average first-5-minute volume.
  9:35 ET  Rank by relative volume in the first 5 minutes, take the top 20.
           Place a buy stop at the opening range high (up bar) or a sell-short
           stop at the low (down bar).
  All day  As soon as an entry fills, place a stop-loss 10% of ATR away. The
           stop is GTC and held by Alpaca, so it survives a crash or a sleeping Mac.
  15:55 ET Cancel anything unfilled and close every ORB position.

Reliability:
  - The scheduler starts it at 8:30 ET. Positions left over from an earlier
    day are closed at the open. If the picks aren't ready by 9:37, the day is
    skipped rather than chasing a stale breakout.
  - Every Alpaca request times out and retries, and a crash during market
    hours resumes from the saved state.
  Then     Report fills, slippage and profit to orb_trades.txt, orb_fills.csv
           and Discord.

Differences from the backtest:
  - With the free data plan, relative volume and the opening range come from
    IEX (one exchange) instead of the whole market. See ORB_FEED in config.py.
  - Stocks in config.SYMBOLS and anything else the account holds are skipped,
    so the experiment never touches the daily bot's shares.

    python orb_live.py             # run today (the scheduler starts it at 8:30 ET)
    python orb_live.py --dry-run   # show the picks for the last trading day, no orders
"""

import argparse
import csv
import functools
import json
import logging
import math
import re
import sys
import time
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetAssetsRequest, GetCalendarRequest, GetOrdersRequest, MarketOrderRequest, StopOrderRequest
from dotenv import load_dotenv

import config
import notify
from bot import BOT_DIR, get_client
from orb_stocks_in_play import (LOOKBACK, MAX_LEVERAGE, MIN_ATR, MIN_AVG_VOLUME, MIN_PRICE, MIN_RELATIVE_VOLUME,
                                NOT_COMMON_STOCK, RISK_PER_TRADE, STOP_ATR_FRACTION, TOP_N, data_client, keys)

ET = ZoneInfo("America/New_York")
LIVE_DIR = BOT_DIR / "data" / "orb_live"
JOURNAL = BOT_DIR / "orb_trades.txt"
FILLS = BOT_DIR / "orb_fills.csv"
POLL_SECONDS = 5
# IEX sees a small slice of each stock's volume; ignore stocks it barely sees.
MIN_IEX_OPEN_VOLUME = 500
# Entries have to go in right after 9:35. If setup or the Mac ran late, skip the
# day instead of chasing a breakout that's already stale.
ENTRY_DEADLINE = dtime(9, 37)
# (connect, read) seconds for every Alpaca request, so a dropped connection
# fails fast and gets retried instead of hanging for an hour.
REQUEST_TIMEOUT = (10, 30)
# Order statuses that mean the order is still working.
OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
                 "pending_replace", "held", "done_for_day"}

log = logging.getLogger("orb")


def setup_logging():
    log_dir = BOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.FileHandler(log_dir / "orb.log")]
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def journal(day, text):
    heading = f"===== ORB {day:%A, %B} {day.day}, {day.year} ====="
    existing = JOURNAL.read_text() if JOURNAL.exists() else ""
    with JOURNAL.open("a") as f:
        if heading not in existing:
            f.write(("\n" if existing else "") + heading + "\n")
        f.write(f"  {datetime.now(ET):%I:%M %p} ET  {text}\n")


def alert(title, text="", kind="info"):
    notify.send(f"[ORB] {title}", text, kind=kind)


def wait_until(moment):
    while (left := (moment - datetime.now(ET)).total_seconds()) > 0:
        time.sleep(min(left, 30))


def with_timeout(client):
    """Make every request from this Alpaca client give up after REQUEST_TIMEOUT."""
    client._session.request = functools.partial(client._session.request, timeout=REQUEST_TIMEOUT)
    return client


def retry(fn, *args, attempts=6, **kwargs):
    """Call fn, retrying network errors (not Alpaca rejections) with backoff."""
    for attempt in range(attempts):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.RequestException as e:
            if attempt == attempts - 1:
                raise
            log.warning(f"{getattr(fn, '__name__', 'request')} failed ({str(e)[:80]}), retrying")
            time.sleep(min(2 ** attempt, 20))


def submit(trading, request):
    """Submit an order, retrying network errors. Every ORB order has its own
    client_order_id, so if a retry finds the first attempt already went
    through, return that order instead of placing a second one."""
    try:
        return retry(trading.submit_order, request)
    except APIError as e:
        if request.client_order_id and "client_order_id" in str(e).lower():
            return retry(trading.get_order_by_client_id, request.client_order_id)
        raise


def cancel(trading, order_id):
    try:
        retry(trading.cancel_order_by_id, order_id)
    except APIError:
        pass  # already filled, canceled or expired


def too_late():
    return datetime.now(ET).time() > ENTRY_DEADLINE


def skip_day(day, why):
    log.info(f"Skipping today: {why}")
    journal(day, f"Skipped: {why}")
    alert("Skipped today", why, kind="warning")


def feed():
    return DataFeed.SIP if config.ORB_FEED == "sip" else DataFeed.IEX


def bars(data, symbols, timeframe, start, end, data_feed, chunk=400):
    """Bars for many symbols, chunked, with retries."""
    frames = []
    for i in range(0, len(symbols), chunk):
        for attempt in range(5):
            try:
                df = data.get_stock_bars(StockBarsRequest(
                    symbol_or_symbols=symbols[i:i + chunk], timeframe=timeframe, start=start, end=end,
                    feed=data_feed, adjustment=Adjustment.RAW)).df
                if len(df):
                    frames.append(df.reset_index())
                break
            except Exception as e:
                log.warning(f"bars request failed ({str(e)[:80]}), retrying")
                time.sleep(10 * (attempt + 1))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def trading_days(trading, before, count):
    cal = retry(trading.get_calendar, GetCalendarRequest(start=before - timedelta(days=count * 2 + 10), end=before - timedelta(days=1)))
    return [d.date for d in cal][-count:]


# --- 9:15: today's candidates -----------------------------------------------

def prepare(trading, data, day):
    """Stocks passing the volume/ATR filters, with their ATR and average
    first-5-minute volume over the last 14 trading days."""
    raw = with_timeout(TradingClient(*keys(), paper=True, raw_data=True))
    assets = retry(raw.get_all_assets, GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE))
    held = {p.symbol for p in retry(trading.get_all_positions)}
    skip = set(config.SYMBOLS) | held
    shortable, symbols = {}, []
    for a in assets:
        if (a["exchange"] in ("NYSE", "NASDAQ", "AMEX") and a["tradable"] and a["symbol"] not in skip
                and re.fullmatch(r"[A-Z]{1,5}", a["symbol"]) and not NOT_COMMON_STOCK.search(a["name"] or "")):
            symbols.append(a["symbol"])
            shortable[a["symbol"]] = bool(a["shortable"] and a["easy_to_borrow"])
    log.info(f"{len(symbols)} tradable common stocks (skipping {len(skip)} already held or used by the daily bot)")

    # Full-market daily bars up to yesterday (old enough for the free plan).
    start = datetime.combine(day - timedelta(days=45), dtime(0), ET)
    daily = bars(data, symbols, TimeFrame.Day, start, datetime.combine(day, dtime(0), ET), DataFeed.SIP, chunk=200)
    daily = daily.sort_values(["symbol", "timestamp"])
    stats = {}
    for symbol, g in daily.groupby("symbol"):
        if len(g) < LOOKBACK + 1:
            continue
        prev_close = g.close.shift(1)
        true_range = pd.concat([g.high - g.low, (g.high - prev_close).abs(), (g.low - prev_close).abs()], axis=1).max(axis=1)
        atr, avg_volume = true_range.tail(LOOKBACK).mean(), g.volume.tail(LOOKBACK).mean()
        if avg_volume >= MIN_AVG_VOLUME and atr > MIN_ATR and g.close.iloc[-1] > MIN_PRICE * 0.8:
            stats[symbol] = {"atr": float(atr), "shortable": shortable[symbol]}
    log.info(f"{len(stats)} pass the volume and ATR filters")

    # Average opening volume, from the same feed the 9:35 ranking will use.
    opens = []
    for d in trading_days(trading, day, LOOKBACK):
        t = datetime.combine(d, dtime(9, 30), ET)
        opens.append(bars(data, list(stats), TimeFrame(5, TimeFrameUnit.Minute), t, t + timedelta(seconds=59), feed()))
    opens = pd.concat([o for o in opens if len(o)], ignore_index=True)
    counts = opens.groupby("symbol").volume.agg(["mean", "count"])
    for symbol, row in counts.iterrows():
        if row["count"] >= 10 and row["mean"] >= MIN_IEX_OPEN_VOLUME:
            stats[symbol]["avg_open_volume"] = float(row["mean"])
    return {s: v for s, v in stats.items() if "avg_open_volume" in v}


# --- 9:35: pick the stocks in play ------------------------------------------

def pick(data, prep, day):
    t = datetime.combine(day, dtime(9, 30), ET)
    first = bars(data, list(prep), TimeFrame(5, TimeFrameUnit.Minute), t, t + timedelta(seconds=59), feed())
    candidates = []
    for row in first.itertuples():
        info = prep[row.symbol]
        direction = 1 if row.close > row.open else -1 if row.close < row.open else 0
        relative_volume = row.volume / info["avg_open_volume"]
        if (row.open > MIN_PRICE and direction and relative_volume >= MIN_RELATIVE_VOLUME
                and (direction == 1 or info["shortable"])):
            candidates.append({
                "symbol": row.symbol, "direction": direction, "relative_volume": round(relative_volume, 2),
                "trigger": round(row.high if direction == 1 else row.low, 2), "atr": info["atr"],
                "risk": STOP_ATR_FRACTION * info["atr"],
            })
    candidates.sort(key=lambda c: c["relative_volume"], reverse=True)
    picks = candidates[:TOP_N]
    for p in picks:
        per_trade_cap = config.ORB_CAPITAL * MAX_LEVERAGE / TOP_N
        p["qty"] = int(math.floor(min(config.ORB_CAPITAL * RISK_PER_TRADE / p["risk"], per_trade_cap / p["trigger"])))
    return [p for p in picks if p["qty"] >= 1]


def order_id(day, symbol, kind):
    return f"orb-{day:%Y%m%d}-{symbol}-{kind}"


def place_entries(trading, picks, day):
    for p in picks:
        side = OrderSide.BUY if p["direction"] == 1 else OrderSide.SELL
        try:
            order = submit(trading, StopOrderRequest(
                symbol=p["symbol"], qty=p["qty"], side=side, time_in_force=TimeInForce.DAY,
                stop_price=p["trigger"], client_order_id=order_id(day, p["symbol"], "entry")))
            p["entry_type"] = "stop"
        except APIError as e:
            if "stop price must be" not in str(e):
                log.warning(f"{p['symbol']}: entry failed: {e}")
                p["entry_type"] = f"failed: {str(e)[:80]}"
                continue
            # The price is already past the trigger. The backtest enters at
            # the market in that case, so do the same.
            log.info(f"{p['symbol']}: stop entry rejected ({e}), entering at market")
            try:
                order = submit(trading, MarketOrderRequest(
                    symbol=p["symbol"], qty=p["qty"], side=side, time_in_force=TimeInForce.DAY,
                    client_order_id=order_id(day, p["symbol"], "entry-mkt")))
                p["entry_type"] = "market (already past trigger)"
            except APIError as e2:
                log.warning(f"{p['symbol']}: entry failed: {e2}")
                p["entry_type"] = f"failed: {str(e2)[:80]}"
                continue
        p["entry_order"] = str(order.id)
        p.update(stop_order=None, stop_price=None, stop_qty=0, stop_n=0)


# --- During the day: protect each fill with a stop-loss ---------------------

def todays_orders(trading, day):
    orders = retry(trading.get_orders, GetOrdersRequest(
        status=QueryOrderStatus.ALL, after=datetime.combine(day, dtime(9, 0), ET), limit=500))
    return {str(o.id): o for o in orders if (o.client_order_id or "").startswith(f"orb-{day:%Y%m%d}-")}


def protect(trading, p, entry, day):
    """Once an entry has finished filling, place its stop-loss.

    The stop is GTC and held by Alpaca, so it keeps protecting the position
    even if this script crashes or the Mac sleeps. close_all, or the next
    morning's flatten_leftovers, cancels it."""
    if p["stop_order"] or not float(entry.filled_qty or 0):
        return
    status = entry.status.value
    if status == "partially_filled":
        # A stop placed while the rest of the entry is still working gets
        # rejected as a wash trade. Give the entry 30 seconds, then cancel the rest.
        if time.time() - p.setdefault("partial_since", time.time()) > 30:
            cancel(trading, entry.id)
        return
    if status in OPEN_STATUSES or status == "pending_cancel":
        return
    filled = float(entry.filled_qty)
    p["entry_fill"] = float(entry.filled_avg_price)
    p["stop_price"] = round(p["entry_fill"] - p["direction"] * p["risk"], 2)
    side = OrderSide.SELL if p["direction"] == 1 else OrderSide.BUY
    p["stop_n"] += 1
    try:
        order = submit(trading, StopOrderRequest(
            symbol=p["symbol"], qty=filled, side=side, time_in_force=TimeInForce.GTC,
            stop_price=p["stop_price"], client_order_id=order_id(day, p["symbol"], f"stop{p['stop_n']}")))
    except APIError as e:
        if "wash trade" in str(e).lower():
            log.info(f"{p['symbol']}: stop not accepted yet ({str(e)[:100]}), trying again next check")
            return
        if "stop price must be" not in str(e):
            raise
        # The price already moved through the stop before it could be placed.
        log.info(f"{p['symbol']}: stop rejected ({e}), exiting at market")
        order = submit(trading, MarketOrderRequest(
            symbol=p["symbol"], qty=filled, side=side, time_in_force=TimeInForce.DAY,
            client_order_id=order_id(day, p["symbol"], f"stopmkt{p['stop_n']}")))
    p["stop_order"], p["stop_qty"] = str(order.id), filled
    log.info(f"{p['symbol']}: entry filled {filled:g} at {p['entry_fill']:.2f}, stop at {p['stop_price']:.2f}")


def monitor(trading, picks, day, until, state_path):
    while datetime.now(ET) < until:
        try:
            orders = todays_orders(trading, day)
            for p in picks:
                entry = orders.get(p.get("entry_order"))
                if entry is not None:
                    protect(trading, p, entry, day)
            save(state_path, picks)
        except Exception:
            log.exception("monitor loop error")
        time.sleep(POLL_SECONDS)


# --- 15:55: close everything and report -------------------------------------

def open_positions(trading, symbols):
    positions = {}
    for symbol in symbols:
        try:
            positions[symbol] = retry(trading.get_open_position, symbol)
        except APIError:
            pass  # no position
    return positions


def close_all(trading, picks, day, close_time):
    """Cancel today's ORB orders and close every ORB position at the market,
    retrying until everything is flat or the market closes."""
    symbols = {p["symbol"] for p in picks}
    if datetime.now(ET) >= close_time:
        left = open_positions(trading, symbols)
        if left:
            # Too late to trade. Leave the GTC stops in place; tomorrow's run
            # closes these at the open.
            text = f"The script wasn't running at the close (was the Mac asleep?). Still open, with stops: {', '.join(sorted(left))}. They'll be closed at tomorrow's open."
            journal(day, text)
            alert("Missed the close", text, kind="warning")
        return
    while datetime.now(ET) < close_time:
        try:
            orders = list(todays_orders(trading, day).values())
            closes = [o for o in orders if "-close" in o.client_order_id]
            close_ids = {o.id for o in closes}
            working = [o for o in orders if o.id not in close_ids and o.status.value in OPEN_STATUSES]
            for o in working:
                cancel(trading, o.id)
            if working or any(o.status.value == "pending_cancel" for o in orders):
                time.sleep(2)  # wait for the cancels so the stops stop holding the shares
                continue
            left = open_positions(trading, symbols)
            if not left:
                return
            for symbol, position in left.items():
                mine = [o for o in closes if o.symbol == symbol]
                if any(o.status.value in OPEN_STATUSES for o in mine):
                    continue  # a close order is already working
                qty = abs(float(position.qty))
                side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
                submit(trading, MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                                                   client_order_id=order_id(day, symbol, f"close{len(mine) + 1}")))
        except Exception:
            log.exception("close-out error, retrying")
        time.sleep(5)
    left = open_positions(trading, symbols)
    if left:
        alert("Couldn't close every position", f"Still open after the close: {', '.join(sorted(left))}. "
              "They'll be closed at tomorrow's open; check Alpaca.", kind="error")


def results(trading, picks, day):
    orders = todays_orders(trading, day).values()
    rows = []
    for p in picks:
        mine = [o for o in orders if o.client_order_id.startswith(order_id(day, p["symbol"], ""))]
        entries = [o for o in mine if "-entry" in o.client_order_id and float(o.filled_qty or 0) > 0]
        if not entries:
            continue
        entry = entries[0]
        qty, entry_price = float(entry.filled_qty), float(entry.filled_avg_price)
        exits = [o for o in mine if o not in entries and float(o.filled_qty or 0) > 0]
        exit_qty = sum(float(o.filled_qty) for o in exits)
        exit_value = sum(float(o.filled_qty) * float(o.filled_avg_price) for o in exits)
        exit_price = exit_value / exit_qty if exit_qty else float("nan")
        stopped = any("-stop" in o.client_order_id for o in exits)
        d = p["direction"]
        stop_slip = ""
        if stopped and p.get("stop_price"):
            stop_fill = next(float(o.filled_avg_price) for o in exits if "-stop" in o.client_order_id)
            stop_slip = round((p["stop_price"] - stop_fill) * d * 100, 2)
        rows.append({
            "date": str(day), "symbol": p["symbol"], "direction": "long" if d == 1 else "short",
            "relative_volume": p["relative_volume"], "entry_type": p["entry_type"], "trigger": p["trigger"],
            "entry_fill": round(entry_price, 4), "entry_slip_cents": round((entry_price - p["trigger"]) * d * 100, 2),
            "qty": qty, "stop_price": p.get("stop_price"), "exit": "stop" if stopped else "close",
            "exit_fill": round(exit_price, 4), "stop_slip_cents": stop_slip,
            "pnl": round(d * (exit_value - qty * entry_price), 2) if exit_qty else "",
        })
    return rows


def report(day, picks, rows):
    new_file = not FILLS.exists()
    if rows:
        with FILLS.open("a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            if new_file:
                writer.writeheader()
            writer.writerows(rows)

    pnl = sum(r["pnl"] for r in rows if r["pnl"] != "")
    wins = sum(1 for r in rows if r["pnl"] != "" and r["pnl"] > 0)
    entry_slips = [r["entry_slip_cents"] for r in rows if r["entry_type"] == "stop"]
    stop_slips = [r["stop_slip_cents"] for r in rows if r["stop_slip_cents"] != ""]
    avg = lambda xs: sum(xs) / len(xs) if xs else float("nan")

    lines = [f"**Profit/loss:** {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} ({pnl / config.ORB_CAPITAL:+.2%} of ${config.ORB_CAPITAL:,})",
             f"**Trades:** {len(rows)} of {len(picks)} picks broke out  |  **Wins:** {wins}",
             f"**Slippage:** entries {avg(entry_slips):+.1f}¢/share, stops {avg(stop_slips):+.1f}¢/share "
             f"(positive = worse than the trigger; the backtest assumed 1¢)", ""]
    for r in sorted(rows, key=lambda r: r["pnl"] if r["pnl"] != "" else 0, reverse=True):
        lines.append(f"{'🟢' if r['pnl'] != '' and r['pnl'] > 0 else '🔴'} {r['symbol']} {r['direction']} {r['qty']:g} "
                     f"@ {r['entry_fill']:.2f} -> {r['exit_fill']:.2f} ({r['exit']})  {r['pnl']:+,.2f}")
        journal(day, f"{r['symbol']} {r['direction'].upper()} {r['qty']:g}: in at ${r['entry_fill']:.2f} "
                     f"(trigger ${r['trigger']:.2f}, {r['entry_slip_cents']:+.1f}c), out at ${r['exit_fill']:.2f} by {r['exit']}"
                     f"  -  {'PROFIT' if r['pnl'] != '' and r['pnl'] >= 0 else 'LOSS'} ${abs(r['pnl'] or 0):,.2f}")

    if FILLS.exists():
        history = pd.read_csv(FILLS)
        lines += ["", f"**Since the experiment started:** {history.date.nunique()} days, {len(history)} trades, "
                      f"{'+' if history.pnl.sum() >= 0 else '-'}${abs(history.pnl.sum()):,.2f}, "
                      f"average entry slippage {history.entry_slip_cents.mean():+.1f}¢"]
    journal(day, f"Day total: {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f} on {len(rows)} trades")
    alert(f"End of day - {day:%a %b %d}", "\n".join(lines), kind="profit" if pnl >= 0 else "loss")


def save(path, picks):
    path.write_text(json.dumps(picks, indent=2, default=str))


# --- Main -------------------------------------------------------------------

def flatten_leftovers(trading, day):
    """Close ORB positions left over from earlier days (the Mac slept through
    the close, or the script crashed). Their GTC stops are canceled first,
    because a stop holds the shares and the closing order would be rejected.
    Run before the open, the closing orders execute at the opening price."""
    stale = [o for o in retry(trading.get_orders, GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
             if (o.client_order_id or "").startswith("orb-") and not o.client_order_id.startswith(f"orb-{day:%Y%m%d}-")]
    symbols = {o.symbol for o in stale}
    for path in sorted(p for p in LIVE_DIR.glob("*.json") if p.stem < str(day))[-3:]:
        symbols |= {p["symbol"] for p in json.loads(path.read_text())}
    symbols -= set(config.SYMBOLS)  # never touch the daily bot's shares
    for o in stale:
        cancel(trading, o.id)
    for _ in range(12):
        ids = {o.id for o in stale}
        if not [o for o in retry(trading.get_orders, GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)) if o.id in ids]:
            break
        time.sleep(5)
    for symbol, position in open_positions(trading, symbols).items():
        qty = abs(float(position.qty))
        side = OrderSide.SELL if float(position.qty) > 0 else OrderSide.BUY
        try:
            submit(trading, MarketOrderRequest(symbol=symbol, qty=qty, side=side, time_in_force=TimeInForce.DAY,
                                               client_order_id=f"orbfix-{day:%Y%m%d}-{symbol}"))
        except APIError as e:
            alert("Couldn't close a leftover position", f"{symbol}: {e}", kind="error")
            continue
        alert("Closing a leftover position", f"{symbol}: {qty:g} shares from an earlier day, closing at the open.", kind="warning")
        journal(day, f"Closing leftover {symbol} position ({qty:g} shares)")


def run(dry_run):
    trading = with_timeout(get_client())
    data = with_timeout(data_client())
    now = datetime.now(ET)
    LIVE_DIR.mkdir(parents=True, exist_ok=True)

    if dry_run:
        day = trading_days(trading, now.date() + timedelta(days=1), 1)[0]
        if now.date() == day and now.time() < dtime(9, 50):
            day = trading_days(trading, now.date(), 1)[0]
        log.info(f"Dry run for {day}")
    else:
        day = now.date()
        log.info(f"Live run for {day}")
        cal = retry(trading.get_calendar, GetCalendarRequest(start=day, end=day))
        if not cal:
            log.info("Market closed today.")
            return
        close_time = cal[0].close.replace(tzinfo=ET)
        flatten_leftovers(trading, day)
        if too_late():
            skip_day(day, f"Started at {now:%H:%M} ET, too late to trade the open (was the Mac asleep?).")
            return

    prep = prepare(trading, data, day)
    log.info(f"{len(prep)} candidates for {day}")

    if dry_run:
        picks = pick(data, prep, day)
        for p in picks:
            log.info(f"would {'buy' if p['direction'] == 1 else 'short'} {p['qty']} {p['symbol']} on a break of "
                     f"{p['trigger']:.2f}, stop {STOP_ATR_FRACTION:.0%} of ATR (${p['risk']:.2f}), relative volume {p['relative_volume']:.1f}x")
        print(f"{len(prep)} candidates, {len(picks)} picks for {day} (dry run, no orders)")
        for p in picks:
            print(f"  {p['symbol']:<6}{'LONG ' if p['direction'] == 1 else 'SHORT'} {p['qty']:>5} on a break of {p['trigger']:>8.2f}"
                  f"   stop ${p['risk']:.2f} away   relative volume {p['relative_volume']:.1f}x")
        return

    if too_late():
        skip_day(day, f"Setup didn't finish until {datetime.now(ET):%H:%M} ET, too late to trade the open.")
        return
    wait_until(datetime.combine(day, dtime(9, 35, 10), ET))
    picks = pick(data, prep, day)
    if too_late():
        skip_day(day, f"Picks weren't ready until {datetime.now(ET):%H:%M} ET, too late to trade the open.")
        return
    place_entries(trading, picks, day)
    state_path = LIVE_DIR / f"{day}.json"
    save(state_path, picks)
    lines = [f"{'🔼' if p['direction'] == 1 else '🔽'} **{p['symbol']}** {'long' if p['direction'] == 1 else 'short'} "
             f"{p['qty']} on a break of ${p['trigger']:.2f} (relative volume {p['relative_volume']:.1f}x)"
             + ("" if p.get("entry_type") == "stop" else f" - {p.get('entry_type')}") for p in picks]
    alert(f"{len(picks)} stocks in play", "\n".join(lines) or "No stocks qualified today.")
    journal(day, f"{len(picks)} picks placed: {', '.join(p['symbol'] for p in picks)}")

    finish(trading, picks, day, close_time, state_path)


def finish(trading, picks, day, close_time, state_path):
    """Watch the fills until 5 minutes before the close, then close out and report."""
    monitor(trading, picks, day, close_time - timedelta(minutes=5), state_path)
    close_all(trading, picks, day, close_time)
    report(day, picks, results(trading, picks, day))
    save(state_path, picks)


def resume_after_crash():
    """After a crash during market hours, pick up where we left off from the
    saved state: keep protecting fills, then close out as usual. If this fails
    too, the GTC stops stay in place and tomorrow's run closes what's left."""
    trading = with_timeout(get_client())
    day = datetime.now(ET).date()
    state_path = LIVE_DIR / f"{day}.json"
    cal = retry(trading.get_calendar, GetCalendarRequest(start=day, end=day))
    if not cal or not state_path.exists():
        return
    close_time = cal[0].close.replace(tzinfo=ET)
    if datetime.now(ET) >= close_time - timedelta(minutes=5):
        return
    log.info("Resuming after a crash")
    finish(trading, json.loads(state_path.read_text()), day, close_time, state_path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show the picks for the last trading day without placing orders")
    args = parser.parse_args()
    load_dotenv(BOT_DIR / ".env")
    setup_logging()
    if not config.ORB_ENABLED and not args.dry_run:
        return
    try:
        run(args.dry_run)
    except Exception:
        log.exception("ORB run failed")
        if args.dry_run:
            sys.exit(1)
        alert("ERROR", "The ORB experiment crashed, see logs/orb.log. Trying to resume.", kind="error")
        try:
            resume_after_crash()
        except Exception:
            log.exception("Resume failed")
            alert("ERROR", "Resuming failed too. Open positions keep their stop-losses and will be closed at "
                  "tomorrow's open. Check Alpaca.", kind="error")
        sys.exit(1)


if __name__ == "__main__":
    main()
