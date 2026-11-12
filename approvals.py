"""Trades you approve: opening-range breakouts found by the Day Trader skill
pack's tools (vendor/day_trader), placed only when you press Approve on the
dashboard.

  9:20 ET    Close any approved position left over from an earlier day (at
             the open).
  10:15 ET   Scan every US stock for a 5-minute close above its 15-minute
             opening range (scan.py). For each breakout, plan a stop at the
             range midpoint and a 2x target (exit.py), then replay the rest
             of the day 10,000 times from the stock's last 60 days and re-run
             the same rule on each of those days (timemachine.py). The best
             few, sized so the stop loses about $100, go to the dashboard and
             Discord.
  Until 3:30 Approve re-prices the trade at the latest price, checks the
             day's limits and places one bracket order: buy at the market
             with the stop-loss and the target attached. Deny drops it.
  3:55 ET    Cancel what's left and close every approved position.

Limits are in config.py (APPROVAL_*). Long only, no overnight holds.

    python approvals.py                        # the scheduled run
    python approvals.py --replay 2026-11-12    # that day's proposals as a test, no orders
"""

import argparse
import fcntl
import json
import logging
import math
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

import requests
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (GetCalendarRequest, GetOrdersRequest, MarketOrderRequest, StopLossRequest,
                                     TakeProfitRequest)
from dotenv import load_dotenv

import config
import notify
from bot import BOT_DIR, get_client

ET = ZoneInfo("America/New_York")
STATE_DIR = BOT_DIR / "approvals"
WORK_DIR = BOT_DIR / "data" / "day_trader"  # the skill tools' trader/ folder
VENDOR = BOT_DIR / "vendor" / "day_trader"
JOURNAL = BOT_DIR / "approval_trades.txt"
DASHBOARD_PORT = 8050
OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled",
                 "pending_cancel", "pending_replace", "held"}

log = logging.getLogger("approvals")


def setup_logging():
    (BOT_DIR / "logs").mkdir(exist_ok=True)
    handler = logging.FileHandler(BOT_DIR / "logs" / "approvals.log")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    if sys.stderr.isatty():
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)


def hhmm(text):
    h, m = text.split(":")
    return dtime(int(h), int(m))


def money(x):
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def journal(day, text):
    heading = f"===== Approvals {day:%A, %B} {day.day}, {day.year} ====="
    existing = JOURNAL.read_text() if JOURNAL.exists() else ""
    with JOURNAL.open("a") as f:
        if heading not in existing:
            f.write(("\n" if existing else "") + heading + "\n")
        f.write(f"  {datetime.now(ET):%I:%M %p} ET  {text}\n")


def alert(title, text="", kind="info"):
    notify.send(f"[Approvals] {title}", text, kind=kind)


def dashboard_url():
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True).stdout.strip()
    except OSError:
        name = ""
    return f"http://{name}.local:{DASHBOARD_PORT}" if name else f"http://localhost:{DASHBOARD_PORT}"


# --- State: one JSON file a day, shared with the dashboard -------------------

def state_path(day):
    return STATE_DIR / f"{day}.json"


def load(day):
    path = state_path(day)
    return json.loads(path.read_text()) if path.exists() else None


@contextmanager
def locked(day):
    """Read, change and save the day's state while holding a lock, so the
    scheduled run and the dashboard never overwrite each other."""
    STATE_DIR.mkdir(exist_ok=True)
    with open(STATE_DIR / f"{day}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load(day) or {"date": str(day), "status": "waiting", "note": "", "proposals": []}
        yield state
        tmp = state_path(day).with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, default=str))
        os.replace(tmp, state_path(day))


# --- Alpaca helpers ----------------------------------------------------------

def data_client():
    return StockHistoricalDataClient(os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"])


def latest_prices(symbols):
    if not symbols:
        return {}
    trades = data_client().get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=list(symbols), feed=DataFeed.IEX))
    return {s: float(t.price) for s, t in trades.items()}


def busy_symbols(trading):
    """Symbols the account already holds or has orders working in. Approved
    trades stay out of them so they never mix with the other bots' shares."""
    held = {p.symbol for p in trading.get_all_positions()}
    working = {o.symbol for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))}
    return held | working | set(config.SYMBOLS)


def market_hours(trading, day):
    cal = trading.get_calendar(GetCalendarRequest(start=day, end=day))
    if not cal:
        return None
    close = cal[0].close.replace(tzinfo=ET)
    flat_by = min(datetime.combine(day, hhmm(config.APPROVAL_FLAT_BY), ET), close - timedelta(minutes=5))
    last_entry = min(datetime.combine(day, hhmm(config.APPROVAL_LAST_ENTRY), ET), close - timedelta(minutes=30))
    return close, flat_by, last_entry


def trade_result(trading, p, day):
    """Entry, exit and profit for one placed trade, from Alpaca's fills."""
    start = datetime.combine(day, dtime(0, 0), ET)
    orders = trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, symbols=[p["symbol"]], after=start,
                                                 limit=100, nested=True))
    flat = []
    for o in orders:
        flat.append(o)
        flat += list(o.legs or [])
    buys = [o for o in flat if o.side == OrderSide.BUY and float(o.filled_qty or 0)]
    sells = [o for o in flat if o.side == OrderSide.SELL and float(o.filled_qty or 0)]
    bought = sum(float(o.filled_qty) for o in buys)
    if not bought:
        return {"filled": False}
    cost = sum(float(o.filled_qty) * float(o.filled_avg_price) for o in buys)
    sold = sum(float(o.filled_qty) for o in sells)
    proceeds = sum(float(o.filled_qty) * float(o.filled_avg_price) for o in sells)
    result = {"filled": True, "qty": bought, "entry_fill": round(cost / bought, 4), "sold": sold}
    if sold >= bought:
        last = max(sells, key=lambda o: o.filled_at)
        kind = {"limit": "target", "stop": "stop"}.get(last.order_type.value, "close")
        result.update(open=False, exit_fill=round(proceeds / sold, 4), exit=kind, pl=round(proceeds - cost, 2))
    else:
        try:
            position = trading.get_open_position(p["symbol"])
            unrealized = float(position.unrealized_pl)
            price = float(position.current_price)
        except APIError:
            unrealized, price = 0.0, None
        realized = proceeds - cost * (sold / bought) if sold else 0.0
        result.update(open=True, price=price, pl=round(realized + unrealized, 2))
    return result


def day_pl(trading, state, day):
    return sum(trade_result(trading, p, day).get("pl", 0.0) for p in state["proposals"] if p["status"] == "placed")


# --- 10:15: find and plan the trades -----------------------------------------

def tool(script, *args, timeout=900):
    """Run one of the skill pack's tools in the shared trader/ folder."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.run([sys.executable, "-I", str(VENDOR / script), *args], cwd=WORK_DIR,
                          capture_output=True, text=True, timeout=timeout)


def scan(day, now_flag):
    universe = WORK_DIR / "trader" / "universe.txt"
    if not universe.exists() or time.time() - universe.stat().st_mtime > 7 * 86400:
        r = tool("scan.py", "universe")
        if r.returncode:
            raise RuntimeError(f"universe download failed: {(r.stderr or r.stdout).strip()[-300:]}")
    r = tool("scan.py", "orb", "--quiet", *now_flag)
    summary = [line for line in (r.stdout + r.stderr).splitlines() if line.strip()]
    watchlist = WORK_DIR / "trader" / "watchlist.json"
    if r.returncode or not watchlist.exists():
        return [], " ".join(summary[-2:])[:300]
    w = json.loads(watchlist.read_text())
    if w.get("mode") != "orb" or not str(w.get("as_of", "")).startswith(str(day)):
        return [], "The scan didn't produce today's breakouts."
    note = f"{w.get('scanned', '?'):,} charts scanned, {len(w['hits'])} broke out by {config.APPROVAL_SCAN_TIME}."
    return w["hits"], note


def plan_one(hit, day, now_flag):
    """Stop, target and Time Machine odds for one breakout, or None."""
    s = hit["symbol"]
    r = tool("exit.py", "plan", "--symbol", s, "--entry", str(hit["price"]), "--or-high", str(hit["or_high"]),
             "--or-low", str(hit["or_low"]), "--date", str(day), "--time", hit["at"],
             "--target", f"{config.APPROVAL_TARGET_R:g}R", "--flat-by", config.APPROVAL_FLAT_BY, "--json", *now_flag,
             timeout=60)
    if r.returncode:
        log.info(f"{s}: no plan ({(r.stderr or r.stdout).strip()[-150:]})")
        return None
    plan = json.loads(r.stdout)
    r = tool("timemachine.py", "run", "--symbol", s, "--date", str(day), "--entry", str(plan["entry"]),
             "--stop", str(plan["stop"]), "--target", str(plan["target"]), "--entry-time", hit["at"],
             "--until", config.APPROVAL_FLAT_BY, "--json", *now_flag, timeout=120)
    if r.returncode:
        log.info(f"{s}: no Time Machine run ({(r.stderr or r.stdout).strip()[-150:]})")
        return None
    tm = json.loads(r.stdout)
    mc, hist = tm["mc"], tm["history"]
    # Score: the average result of the replays and of the real past days, in
    # units of the stop distance (R). Past days count once there are enough.
    score = (mc["avg_R"] + hist["avg_R"]) / 2 if hist["triggered"] >= 5 else mc["avg_R"]
    return {
        "id": s, "symbol": s, "entry": plan["entry"], "entry_time": hit["at"],
        "or_high": hit["or_high"], "or_low": hit["or_low"], "stop": plan["stop"], "target": plan["target"],
        "risk_per_share": plan["risk_per_share"],
        "odds": {"target": mc["p_target"], "stop": mc["p_stop"], "neither": mc["p_neither"], "avg_R": mc["avg_R"],
                 "median_close": mc["median_close"], "p5_close": mc["p5_close"], "p95_close": mc["p95_close"],
                 "sessions": mc["pool_sessions"], "seed": mc["seed"]},
        "history": {k: hist[k] for k in ("sessions", "triggered", "target", "stop", "close", "avg_R")},
        "verdict": tm["verdict"], "score": round(score, 3),
    }


def size(entry, stop, room=None):
    risk = entry - stop
    max_loss = config.APPROVAL_MAX_LOSS if room is None else min(config.APPROVAL_MAX_LOSS, room)
    return int(math.floor(min(max_loss / risk, config.APPROVAL_MAX_ORDER / entry))) if risk > 0 else 0


def propose(trading, day, now_flag=(), dry_run=False):
    with locked(day) as st:
        st.update(status="scanning", note="Scanning every US stock for opening-range breakouts…", dry_run=dry_run)
    try:
        hits, note = scan(day, now_flag)
        skip = busy_symbols(trading)
        hits = [h for h in hits if h["symbol"] not in skip and h["price"] >= config.APPROVAL_MIN_PRICE]
        log.info(f"{len(hits)} breakouts to plan: {note}")
        with ThreadPoolExecutor(4) as pool:
            plans = [p for p in pool.map(lambda h: plan_one(h, day, now_flag), hits) if p]
    except Exception as e:
        log.exception("scan failed")
        with locked(day) as st:
            st.update(status="failed", note=f"The scan failed: {str(e)[:200]}")
        if not dry_run:
            alert("Scan failed", str(e)[:300], kind="error")
        return []

    # Drop the ones where both the replays and the real past days lost on
    # average, then keep the best few.
    plans = [p for p in plans if p["odds"]["avg_R"] > 0 or p["history"]["avg_R"] > 0]
    tight = [p for p in plans if p["risk_per_share"] < config.APPROVAL_MIN_STOP]
    if tight:
        log.info(f"Skipped, stop under ${config.APPROVAL_MIN_STOP:g}: "
                 + ", ".join(f"{p['symbol']} ${p['risk_per_share']:.2f}" for p in tight))
    plans = [p for p in plans if p["risk_per_share"] >= config.APPROVAL_MIN_STOP]
    plans.sort(key=lambda p: p["score"], reverse=True)
    picks = []
    for p in plans:
        qty = size(p["entry"], p["stop"])
        if qty < 1:
            continue
        p.update(qty=qty, cost=round(qty * p["entry"], 2), max_loss=round(qty * p["risk_per_share"], 2),
                 status="pending", note="")
        picks.append(p)
        if len(picks) == config.APPROVAL_MAX_PROPOSALS:
            break

    with locked(day) as st:
        st.update(status="ready", note=f"{note} {len(plans)} had a stop of at least ${config.APPROVAL_MIN_STOP:g} and an edge in the replays or the past days; "
                                       f"showing the best {len(picks)}.", proposals=picks,
                  proposed_at=datetime.now(ET).isoformat(timespec="seconds"))
    journal(day, f"{len(picks)} proposed: {', '.join(p['symbol'] for p in picks) or 'none'}")
    if not dry_run:
        lines = [f"**{p['symbol']}** buy {p['qty']} near ${p['entry']:,.2f} · stop ${p['stop']:,.2f} · target "
                 f"${p['target']:,.2f} · risk {money(p['max_loss'])}\n"
                 f"   replays: target first {p['odds']['target']:.0%}, stop first {p['odds']['stop']:.0%} · "
                 f"past days: {p['history']['target']} target, {p['history']['stop']} stop of {p['history']['triggered']}"
                 for p in picks]
        text = "\n".join(lines) or "No breakout had an edge today. Nothing to approve."
        alert(f"{len(picks)} trades waiting for you", f"{text}\n\nApprove or deny: {dashboard_url()}")
    return picks


# --- Approve / deny (called by the dashboard) --------------------------------

def find(state, pid):
    return next((p for p in state["proposals"] if p["id"] == pid), None)


def approve(day, pid, trading=None):
    """Place one approved trade. Returns (ok, message)."""
    trading = trading or get_client()
    with locked(day) as st:
        p = find(st, pid)
        if not p:
            return False, "That trade isn't in today's list."
        if p["status"] != "pending":
            return False, f"{p['symbol']} is already {p['status']}."
        dry_run = st.get("dry_run", False)
        if not dry_run:
            hours = market_hours(trading, day)
            now = datetime.now(ET)
            if day != now.date() or not hours:
                return False, "That's not today's trade."
            if now >= hours[2]:
                p.update(status="expired", note="Too late in the day for a new entry.")
                return False, p["note"]
            if not trading.get_clock().is_open:
                return False, "The market is closed."
        placed = [x for x in st["proposals"] if x["status"] == "placed"]
        if len(placed) >= config.APPROVAL_MAX_TRADES:
            return False, f"Already {len(placed)} trades today, the daily limit (APPROVAL_MAX_TRADES)."
        pl = 0.0 if dry_run else day_pl(trading, st, day)
        room = config.APPROVAL_MAX_DAILY_LOSS + min(pl, 0.0)
        if room <= 0:
            return False, f"Today's approved trades are down {money(-pl)}, the daily limit. No new trades today."
        if not dry_run and p["symbol"] in busy_symbols(trading):
            return False, f"The account already holds or has orders in {p['symbol']}."

        price = p["entry"] if dry_run else latest_prices([p["symbol"]])[p["symbol"]]
        if price <= p["stop"]:
            p.update(status="expired", note=f"Fell to ${price:,.2f}, through the stop before it was approved.")
            return False, p["note"]
        if price >= p["target"]:
            p.update(status="expired", note=f"Already ran to ${price:,.2f}, past the target.")
            return False, p["note"]
        reward_risk = (p["target"] - price) / (price - p["stop"])
        if reward_risk < config.APPROVAL_MIN_REWARD_RISK:
            return False, (f"{p['symbol']} is at ${price:,.2f}, too close to the target: only {reward_risk:.1f}x the "
                           f"risk left. It stays on the list in case it pulls back.")
        qty = size(price, p["stop"], room)
        if not dry_run:
            qty = min(qty, int(float(trading.get_account().buying_power) // price))
        if qty < 1:
            return False, "Not enough room left today (daily loss limit or buying power) for even one share."

        client_id = f"appr-{day:%Y%m%d}-{p['symbol']}"
        if dry_run:
            order_id = None
        else:
            request = MarketOrderRequest(symbol=p["symbol"], qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                         order_class=OrderClass.BRACKET, client_order_id=client_id,
                                         take_profit=TakeProfitRequest(limit_price=round(p["target"], 2)),
                                         stop_loss=StopLossRequest(stop_price=round(p["stop"], 2)))
            try:
                order = trading.submit_order(request)
            except requests.RequestException:
                # The connection dropped; Alpaca may still have the order.
                try:
                    order = trading.get_order_by_client_id(client_id)
                except Exception:
                    return False, "Lost the connection placing the order, and Alpaca has no record of it. Try again."
            except APIError as e:
                p.update(note=f"Alpaca refused: {str(e)[:150]}")
                return False, p["note"]
            order_id = str(order.id)
        p.update(status="placed", approved_at=datetime.now(ET).isoformat(timespec="seconds"), approved_price=price,
                 qty=qty, max_loss=round(qty * (price - p["stop"]), 2), cost=round(qty * price, 2),
                 order_id=order_id, client_order_id=client_id,
                 note="")
        message = (f"Bought {qty} {p['symbol']} at the market (last ${price:,.2f}), stop ${p['stop']:,.2f}, "
                   f"target ${p['target']:,.2f}. Max loss about {money(p['max_loss'])}.")
    if dry_run:
        return True, "Test run, no order sent. " + message
    journal(day, f"APPROVED {message}")
    alert(f"✅ Approved {p['symbol']}", message, kind="profit")
    return True, message


def deny(day, pid):
    with locked(day) as st:
        p = find(st, pid)
        if not p:
            return False, "That trade isn't in today's list."
        if p["status"] != "pending":
            return False, f"{p['symbol']} is already {p['status']}."
        p.update(status="denied", note="", denied_at=datetime.now(ET).isoformat(timespec="seconds"))
        dry_run = st.get("dry_run", False)
    if not dry_run:
        journal(day, f"Denied {p['symbol']}")
    return True, f"Denied {p['symbol']}."


# --- During the day and at 3:55 ----------------------------------------------

def expire_stale(trading, day, last_entry):
    """Take pending trades off the list once they can't be taken any more."""
    with locked(day) as st:
        pending = [p for p in st["proposals"] if p["status"] == "pending"]
        if not pending:
            return
        if datetime.now(ET) >= last_entry:
            for p in pending:
                p.update(status="expired", note="Not approved before the last entry time.")
            return
        try:
            prices = latest_prices({p["symbol"] for p in pending})
        except Exception as e:
            log.warning(f"price check failed: {str(e)[:100]}")
            return
        for p in pending:
            price = prices.get(p["symbol"])
            if price is not None and price <= p["stop"]:
                p.update(status="expired", note=f"Fell to ${price:,.2f}, through the stop, before it was approved.")
            elif price is not None and price >= p["target"]:
                p.update(status="expired", note=f"Ran to ${price:,.2f}, past the target, before it was approved.")


def close_out(trading, day, close_time):
    """Cancel the approved trades' working orders (their stop and target) and
    sell every approved position at the market."""
    symbols = {p["symbol"] for p in (load(day) or {"proposals": []})["proposals"] if p["status"] == "placed"}
    if not symbols:
        return
    attempt = 0
    while datetime.now(ET) < close_time:
        try:
            working = [o for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500))
                       if o.symbol in symbols and "-close" not in (o.client_order_id or "")]
            for o in working:
                try:
                    trading.cancel_order_by_id(o.id)
                except APIError:
                    pass
            if working:
                time.sleep(2)
                continue
            held = {p.symbol: p for p in trading.get_all_positions() if p.symbol in symbols and float(p.qty) > 0}
            if not held:
                return
            attempt += 1
            for symbol, position in held.items():
                try:
                    trading.submit_order(MarketOrderRequest(
                        symbol=symbol, qty=float(position.qty), side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
                        client_order_id=f"appr-{day:%Y%m%d}-{symbol}-close{attempt}"))
                except APIError as e:
                    log.warning(f"{symbol}: close refused ({str(e)[:100]})")
            time.sleep(5)
        except Exception:
            log.exception("close-out error, retrying")
            time.sleep(5)
    left = [p.symbol for p in trading.get_all_positions() if p.symbol in symbols]
    if left:
        alert("Couldn't close every approved trade", f"Still open after the close: {', '.join(left)}. "
              "They'll be closed at tomorrow's open.", kind="error")


def report(trading, day):
    with locked(day) as st:
        placed = [p for p in st["proposals"] if p["status"] == "placed"]
        for p in placed:
            p["result"] = trade_result(trading, p, day)
        st["status"] = "closed"
        st["day_pl"] = round(sum(p["result"].get("pl", 0.0) for p in placed), 2)
        others = [p for p in st["proposals"] if p["status"] != "placed"]
    lines = []
    for p in placed:
        r = p["result"]
        if not r.get("filled"):
            lines.append(f"⚪ {p['symbol']}: the buy never filled")
            continue
        lines.append(f"{'🟢' if r['pl'] >= 0 else '🔴'} {p['symbol']} {r['qty']:g} @ {r['entry_fill']:.2f} -> "
                     f"{r.get('exit_fill', 0):.2f} ({r.get('exit', 'open')})  {money(r['pl'])}")
        journal(day, f"{p['symbol']} {r['qty']:g}: in at ${r['entry_fill']:.2f}, out at ${r.get('exit_fill', 0):.2f} "
                     f"by {r.get('exit', 'open')}  -  {'PROFIT' if r['pl'] >= 0 else 'LOSS'} {money(abs(r['pl']))}")
    if others:
        lines.append("Not taken: " + ", ".join(f"{p['symbol']} ({p['status']})" for p in others))
    total = st["day_pl"]
    journal(day, f"Day total: {money(total)} on {len(placed)} approved trades")
    if placed or others:
        alert(f"End of day - {day:%a %b %d}", f"**Profit/loss:** {money(total)} on {len(placed)} approved trades\n"
              + "\n".join(lines), kind="profit" if total >= 0 else "loss")


def flatten_leftovers(trading, day):
    """Sell approved positions left over from an earlier day (the Mac slept
    through 3:55). Run before the open, they sell at the opening price."""
    symbols = set()
    for path in sorted(STATE_DIR.glob("*.json"))[-6:]:
        old = json.loads(path.read_text())
        if path.stem < str(day) and not old.get("dry_run"):
            symbols |= {p["symbol"] for p in old["proposals"] if p["status"] == "placed"}
    symbols -= set(config.SYMBOLS)
    if not symbols:
        return
    for o in trading.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)):
        if o.symbol in symbols:  # a stop or target still holding the shares
            try:
                trading.cancel_order_by_id(o.id)
            except APIError:
                pass
    time.sleep(3)
    for position in trading.get_all_positions():
        if position.symbol in symbols and float(position.qty) > 0:
            try:
                trading.submit_order(MarketOrderRequest(symbol=position.symbol, qty=float(position.qty), side=OrderSide.SELL,
                                                        time_in_force=TimeInForce.DAY,
                                                        client_order_id=f"apprfix-{day:%Y%m%d}-{position.symbol}"))
                alert("Closing a leftover position", f"{position.symbol}: {float(position.qty):g} shares from an earlier "
                      "day, selling at the open.", kind="warning")
                journal(day, f"Closing leftover {position.symbol} ({float(position.qty):g} shares)")
            except APIError as e:
                alert("Couldn't close a leftover position", f"{position.symbol}: {e}", kind="error")


def wait_until(moment):
    while (left := (moment - datetime.now(ET)).total_seconds()) > 0:
        time.sleep(min(left, 30))


def run():
    trading = get_client()
    day = datetime.now(ET).date()
    hours = market_hours(trading, day)
    if not hours:
        log.info(f"{day}: market closed")
        return
    close_time, flat_by, last_entry = hours
    log.info(f"Live run for {day}")
    flatten_leftovers(trading, day)
    if not config.APPROVALS_ENABLED:
        return
    if datetime.now(ET) >= last_entry:
        log.info("Started after the last entry time, nothing to propose today")
        return
    with locked(day) as st:
        if st["status"] == "waiting":
            st["note"] = f"The scan runs at {config.APPROVAL_SCAN_TIME} ET."
    if (load(day) or {}).get("status") in ("waiting", "failed", "scanning"):
        wait_until(datetime.combine(day, hhmm(config.APPROVAL_SCAN_TIME), ET))
        propose(trading, day)
    while datetime.now(ET) < flat_by:
        try:
            expire_stale(trading, day, last_entry)
        except Exception:
            log.exception("expire check failed")
        time.sleep(30)
    close_out(trading, day, close_time)
    report(trading, day)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replay", metavar="DATE", help="build that day's proposals as a test (no orders, no Discord)")
    args = parser.parse_args()
    load_dotenv(BOT_DIR / ".env")
    setup_logging()
    if args.replay:
        day = date.fromisoformat(args.replay)
        if state_path(day).exists() and not (load(day) or {}).get("dry_run"):
            sys.exit(f"{day} already has a real run; not overwriting it.")
        picks = propose(get_client(), day, ("--now", f"{day} {config.APPROVAL_SCAN_TIME}"), dry_run=True)
        print((load(day) or {}).get("note", ""))
        for p in picks:
            print(f"  {p['symbol']:<6} buy {p['qty']:>4} near {p['entry']:>9.2f}  stop {p['stop']:>9.2f}  target "
                  f"{p['target']:>9.2f}  risk {money(p['max_loss']):>8}  target first {p['odds']['target']:.0%}, stop "
                  f"first {p['odds']['stop']:.0%}  past days {p['history']['target']}/{p['history']['stop']} of "
                  f"{p['history']['triggered']}  score {p['score']:+.2f}")
        return
    try:
        run()
    except Exception as e:
        log.exception("approvals run failed")
        alert("The approvals run crashed", f"{str(e)[:300]}\nAnything already approved keeps its stop and target; "
              "check Alpaca before the close.", kind="error")
        raise


if __name__ == "__main__":
    main()
