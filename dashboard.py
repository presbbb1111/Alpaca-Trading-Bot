"""Live dashboard for the trading bot, at http://localhost:8050

    python dashboard.py

View-only: it reads the Alpaca account and the bot's files and never places
orders. It only listens on this Mac (127.0.0.1). The scheduler keeps it
running in the background; see launchd/com.presleybarr.tradingbot.dashboard.plist.
"""

import json
import logging
import re
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone

import pandas as pd
from alpaca.trading.requests import GetPortfolioHistoryRequest
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

import config
from bot import BOT_DIR, MARKET_TZ, fetch_prices, get_client, is_crypto, load_positions, previous_close_equity
from strategies import STRATEGIES, rsi

PORT = 8050
PRICE_CACHE_SECONDS = 600
JOBS = {
    "Daily bot": ("com.presleybarr.tradingbot", "9:30 AM ET, every day"),
    "ORB experiment": ("com.presleybarr.tradingbot.orb", "9:15 AM ET, market days"),
    "Daily summary": ("com.presleybarr.tradingbot.summary", "4:15 PM ET, every day"),
}

app = Flask(__name__, static_folder=str(BOT_DIR / "web"), static_url_path="")
logging.getLogger("werkzeug").setLevel(logging.WARNING)
load_dotenv(BOT_DIR / ".env")
_client = None
_price_cache = {}


def client():
    global _client
    if _client is None:
        _client = get_client()
    return _client


def safe(fn):
    """Run one section; a failure shows as an error in that card only."""
    try:
        return fn()
    except Exception as e:
        logging.exception(fn.__name__)
        return {"error": str(e)[:200]}


# --- Account and holdings ---------------------------------------------------

def account_section():
    trading = client()
    account = trading.get_account()
    equity = float(account.equity)
    last = previous_close_equity(trading, account)
    positions = trading.get_all_positions()
    invested = sum(float(p.market_value) for p in positions)
    clock = trading.get_clock()
    return {
        "equity": equity, "day_change": equity - last, "day_pct": (equity - last) / last if last else 0,
        "total_change": equity - config.STARTING_BALANCE,
        "total_pct": (equity - config.STARTING_BALANCE) / config.STARTING_BALANCE,
        "cash": float(account.cash), "invested": invested, "invested_pct": invested / equity if equity else 0,
        "buying_power": float(account.buying_power), "open_pl": sum(float(p.unrealized_pl) for p in positions),
        "market_open": clock.is_open, "next_open": clock.next_open.isoformat(), "next_close": clock.next_close.isoformat(),
        "starting_balance": config.STARTING_BALANCE,
    }


def holdings_section():
    rows = []
    for p in client().get_all_positions():
        rows.append({
            "symbol": p.symbol, "qty": float(p.qty), "price": float(p.current_price), "value": float(p.market_value),
            "cost": float(p.cost_basis), "pl": float(p.unrealized_pl), "pl_pct": float(p.unrealized_plpc),
            "today_pct": float(p.change_today or 0), "crypto": p.asset_class.value == "crypto",
        })
    return sorted(rows, key=lambda r: r["value"], reverse=True)


def strategies_section():
    """Profit/loss per strategy, from what each one paid (positions.json)."""
    prices = {p.symbol: float(p.current_price) for p in client().get_all_positions()}
    totals = defaultdict(lambda: {"cost": 0.0, "value": 0.0, "positions": 0})
    for symbol, held in load_positions().items():
        price = prices.get(symbol.replace("/", ""))
        if price is None:
            continue
        for key, h in held.items():
            t = totals[STRATEGIES[key][0].split(" (")[0]]
            t["cost"] += h["cost"]
            t["value"] += h["shares"] * price
            t["positions"] += 1
    rows = [{"name": name, "pl": t["value"] - t["cost"], "pl_pct": (t["value"] - t["cost"]) / t["cost"], **t}
            for name, t in totals.items() if t["cost"]]
    return sorted(rows, key=lambda r: r["pl"], reverse=True)


# --- Signals: where every strategy stands on every symbol -------------------

def closes_for(symbol):
    cached = _price_cache.get(symbol)
    if cached and time.time() - cached[0] < PRICE_CACHE_SECONDS:
        return cached[1]
    closes = fetch_prices(symbol, config.LOOKBACK_DAYS)
    _price_cache[symbol] = (time.time(), closes)
    return closes


def signal_detail(key, closes, holding):
    """A short sentence on how close this strategy is to changing its mind."""
    if key == "crossover":
        short = closes.rolling(config.SHORT_WINDOW).mean().iloc[-1]
        long = closes.rolling(config.LONG_WINDOW).mean().iloc[-1]
        gap = short / long - 1
        return f"{config.SHORT_WINDOW}-day avg {abs(gap):.1%} {'above' if gap >= 0 else 'below'} {config.LONG_WINDOW}-day", gap
    if key == "rsi":
        value = rsi(closes, config.RSI_PERIOD).iloc[-1]
        target = f"sells above {config.RSI_SELL_ABOVE}" if holding else f"buys below {config.RSI_BUY_BELOW}"
        return f"RSI {value:.0f}, {target}", value
    if key == "momentum":
        change = closes.iloc[-1] / closes.iloc[-1 - config.MOMENTUM_LOOKBACK] - 1
        return f"{change:+.1%} over 12 months, rechecked at month end", change
    if key == "breakout":
        if holding:
            low = closes.iloc[-1 - config.BREAKOUT_EXIT:-1].min()
            return f"{closes.iloc[-1] / low - 1:.1%} above its {config.BREAKOUT_EXIT}-day low (sells there)", None
        high = closes.iloc[-1 - config.BREAKOUT_ENTRY:-1].max()
        return f"{1 - closes.iloc[-1] / high:.1%} below its {config.BREAKOUT_ENTRY}-day high (buys there)", None
    return "", None


def signals_section():
    positions = load_positions()
    groups = [(s, config.ENABLED_STRATEGIES) for s in config.SYMBOLS] + [(s, config.CRYPTO_STRATEGIES) for s in config.CRYPTO_SYMBOLS]
    rows = []
    for symbol, keys in groups:
        try:
            closes = closes_for(symbol)
        except Exception as e:
            rows.append({"symbol": symbol, "error": str(e)[:120]})
            continue
        today = datetime.now(timezone.utc if is_crypto(symbol) else MARKET_TZ).date()
        cells = {}
        for key in keys:
            name, fn = STRATEGIES[key]
            holding = key in positions.get(symbol, {})
            want = bool(fn(closes, next_day=today).iloc[-1])
            detail, _ = signal_detail(key, closes, holding)
            cells[key] = {"want": "hold" if want else "cash", "holding": holding, "detail": detail,
                          "pending_change": want != holding}
        rows.append({"symbol": symbol, "price": float(closes.iloc[-1]), "cells": cells})
    return {"strategies": [{"key": k, "name": STRATEGIES[k][0].split(" (")[0]} for k in STRATEGIES], "rows": rows,
            "as_of": datetime.now(MARKET_TZ).strftime("%I:%M %p ET")}


# --- Trades journal ---------------------------------------------------------

def parse_journal(path, limit=40):
    if not path.exists():
        return []
    entries, day = [], None
    for line in path.read_text().splitlines():
        heading = re.match(r"=+ (?:ORB )?(.+?) =+", line.strip())
        if heading:
            day = heading.group(1)
            continue
        m = re.match(r"\s+(\d\d:\d\d [AP]M) ET\s+(.*)", line)
        if not m:
            continue
        text = m.group(2).replace("FILL UPDATE  -  ", "").replace(" filled at ", " at ")
        kind = ("error" if re.search(r"ERROR|FAILED", text) else "profit" if "PROFIT" in text else
                "loss" if "LOSS" in text else "buy" if re.search(r"\bBUY\b", text) else
                "sell" if re.search(r"\bSELL\b", text) else "info")
        if kind == "info" or "not filled yet" in text or "partial fill, see" in text:
            continue
        entries.append({"day": day, "time": m.group(1), "text": text, "kind": kind})
    return entries[::-1][:limit]


# --- ORB experiment ----------------------------------------------------------

def orb_section():
    import orb_live
    fills_path = BOT_DIR / "orb_fills.csv"
    fills = pd.read_csv(fills_path) if fills_path.exists() else pd.DataFrame()
    all_time = None
    if len(fills):
        all_time = {"pnl": float(fills.pnl.sum()), "days": int(fills.date.nunique()), "trades": len(fills),
                    "wins": int((fills.pnl > 0).sum()), "entry_slip": float(fills.entry_slip_cents.mean()),
                    "stop_slip": float(pd.to_numeric(fills.stop_slip_cents, errors="coerce").mean())}

    day = datetime.now(MARKET_TZ).date()
    state_path = orb_live.LIVE_DIR / f"{day}.json"
    if not state_path.exists():
        return {"active": False, "all_time": all_time, "capital": config.ORB_CAPITAL, "enabled": config.ORB_ENABLED}

    picks = json.loads(state_path.read_text())
    trading = client()
    orders = list(orb_live.todays_orders(trading, day).values())
    positions = {p.symbol: p for p in trading.get_all_positions()}
    today_fills = fills[fills.date == str(day)] if len(fills) else pd.DataFrame()
    rows = []
    for p in picks:
        mine = [o for o in orders if o.client_order_id.startswith(orb_live.order_id(day, p["symbol"], ""))]
        entry = next((o for o in mine if "-entry" in o.client_order_id), None)
        stopped = any("-stop" in o.client_order_id and o.status.value == "filled" for o in mine)
        closed = any(o.client_order_id.endswith("-close") and o.status.value == "filled" for o in mine)
        filled = entry is not None and float(entry.filled_qty or 0) > 0
        pl = None
        if p["symbol"] in positions and filled:
            status, pl = "In trade", float(positions[p["symbol"]].unrealized_pl)
        elif stopped:
            status = "Stopped out"
        elif closed:
            status = "Closed"
        elif entry is None:
            status = "Order failed"
        elif not filled and entry.status.value in ("canceled", "expired"):
            status = "No breakout"
        elif not filled:
            status = "Waiting"
        else:
            status = "Closed"
        if pl is None and len(today_fills):
            match = today_fills[today_fills.symbol == p["symbol"]]
            if len(match):
                pl = float(match.pnl.iloc[0])
        rows.append({
            "symbol": p["symbol"], "direction": "long" if p["direction"] == 1 else "short", "qty": p["qty"],
            "trigger": p["trigger"], "stop": p.get("stop_price"), "entry_fill": p.get("entry_fill"),
            "relative_volume": p["relative_volume"], "status": status, "pl": pl,
        })
    return {"active": True, "rows": rows, "all_time": all_time, "capital": config.ORB_CAPITAL,
            "today_pl": sum(r["pl"] for r in rows if r["pl"] is not None), "enabled": config.ORB_ENABLED}


# --- Health -----------------------------------------------------------------

def to_et(local_text):
    """Log timestamps are in the Mac's local time; show them in Eastern."""
    local = datetime.strptime(local_text, "%Y-%m-%d %H:%M:%S").astimezone()
    return local.astimezone(MARKET_TZ).strftime("%a %b %d, %-I:%M %p ET")


def last_log_time(path, pattern):
    if not path.exists():
        return None
    for line in reversed(path.read_text().splitlines()[-5000:]):
        if re.search(pattern, line):
            return to_et(line[:19])
    return None


def health_section():
    logs = BOT_DIR / "logs"
    last_runs = {
        "Daily bot": last_log_time(logs / "bot.log", r"Running on (?!.*dry run)"),
        "ORB experiment": last_log_time(logs / "orb.log", r"Live run for"),
        "Daily summary": (to_et(datetime.fromtimestamp((logs / "summary.out.log").stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S"))
                          if (logs / "summary.out.log").exists() else None),
    }
    jobs = []
    for name, (label, schedule) in JOBS.items():
        out = subprocess.run(["launchctl", "print", f"gui/{_uid()}/{label}"], capture_output=True, text=True).stdout
        exit_code = re.search(r"last exit code = (\S+)", out)
        jobs.append({
            "name": name, "schedule": schedule, "loaded": bool(out),
            "running": "state = running" in out,
            "last_exit": exit_code.group(1) if exit_code else None,
            "last_run": last_runs.get(name),
        })
    today = datetime.now().strftime("%Y-%m-%d")
    errors = []
    for log_name in ("bot.log", "orb.log"):
        path = logs / log_name
        if path.exists():
            errors += [line[20:180] for line in path.read_text().splitlines()[-3000:]
                       if line.startswith(today) and " ERROR " in line]
    return {"jobs": jobs, "errors_today": errors[-5:]}


def _uid():
    import os
    return os.getuid()


# --- Routes -----------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/summary")
def summary():
    return jsonify({
        "updated": datetime.now(MARKET_TZ).strftime("%I:%M:%S %p ET"),
        "account": safe(account_section),
        "holdings": safe(holdings_section),
        "strategies": safe(strategies_section),
        "signals": safe(signals_section),
        "trades": safe(lambda: parse_journal(BOT_DIR / "trades.txt")),
        "orb": safe(orb_section),
        "health": safe(health_section),
    })


RANGES = {"1D": ("1D", "5Min"), "1W": ("1W", "1H"), "1M": ("1M", "1D"), "ALL": ("1A", "1D")}


@app.get("/api/history")
def history():
    period, timeframe = RANGES.get(request.args.get("range", "1W"), RANGES["1W"])

    def load():
        h = client().get_portfolio_history(GetPortfolioHistoryRequest(period=period, timeframe=timeframe, extended_hours=False))
        points = [{"t": t * 1000, "v": v} for t, v in zip(h.timestamp or [], h.equity or []) if v]
        # Alpaca's paper history sometimes reports a day at roughly double the
        # real value (it happened on this account's first day). Drop points
        # more than 40% away from the typical value.
        if points:
            typical = sorted(p["v"] for p in points)[len(points) // 2]
            points = [p for p in points if abs(p["v"] - typical) / typical <= 0.4]
        return {"points": points, "base": config.STARTING_BALANCE}

    return jsonify(safe(load))


if __name__ == "__main__":
    print(f"Dashboard running at http://localhost:{PORT}")
    app.run(host="127.0.0.1", port=PORT, threaded=True)
