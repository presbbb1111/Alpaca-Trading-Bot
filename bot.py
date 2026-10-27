"""Multi-strategy trading bot for an Alpaca paper account.

Run once:             python bot.py
Test without trading: python bot.py --dry-run

Every strategy enabled in config.py trades every stock (and coin) with its own
money. What each one owns is tracked in positions.json, so one strategy's sell
never touches another strategy's shares. Stocks trade on market days only;
crypto trades every day. Each run is logged to logs/bot.log, and trades are
written to trades.txt.
"""

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderStatus, TimeInForce
from alpaca.trading.requests import GetCalendarRequest, MarketOrderRequest
from dotenv import load_dotenv

import config
import notify
from strategies import STRATEGIES, describe

BOT_DIR = Path(__file__).resolve().parent
POSITIONS_FILE = BOT_DIR / "positions.json"
MARKET_TZ = ZoneInfo("America/New_York")
DONE_STATUSES = (OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED)
FAILED_STATUSES = (OrderStatus.REJECTED, OrderStatus.CANCELED, OrderStatus.EXPIRED)

log = logging.getLogger("bot")

# Everything journaled during this run, for the end-of-run Discord summary.
run_lines = []


def setup_logging():
    log_dir = BOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handlers = [logging.FileHandler(log_dir / "bot.log")]
    # Also print to the screen when run by hand (not from the scheduler).
    if sys.stderr.isatty():
        handlers.append(logging.StreamHandler())
    for handler in handlers:
        handler.setFormatter(formatter)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def journal(text, alert=None):
    """Add a line to trades.txt under today's date heading.

    `alert` ("buy", "profit", "loss", "error", "warning") also sends the line
    to Discord right away. Everything else goes in the end-of-run summary.
    """
    run_lines.append(text)
    if alert:
        notify.send(text, kind=alert)
    path = BOT_DIR / "trades.txt"
    now = datetime.now(MARKET_TZ)
    heading = f"===== {now:%A, %B} {now.day}, {now.year} ====="
    existing = path.read_text() if path.exists() else ""
    with path.open("a") as f:
        if heading not in existing:
            f.write(("\n" if existing else "") + heading + "\n")
        f.write(f"  {now:%I:%M %p} ET  {text}\n")


def load_positions():
    """Shares and total cost per symbol and strategy,
    e.g. {"AAPL": {"rsi": {"shares": 8, "cost": 2716.0}}}."""
    if POSITIONS_FILE.exists():
        return json.loads(POSITIONS_FILE.read_text())
    return {}


def save_positions(positions):
    POSITIONS_FILE.write_text(json.dumps(positions, indent=2) + "\n")


def is_crypto(symbol):
    return "/" in symbol


def fetch_prices(symbol, days):
    """Return the last `days` completed daily closing prices."""
    # Ask for extra calendar days so we get `days` full trading days after
    # weekends and holidays are removed.
    start = datetime.now() - timedelta(days=int(days * 1.6) + 10)
    # Yahoo writes crypto as BTC-USD; Alpaca writes it as BTC/USD.
    history = yf.Ticker(symbol.replace("/", "-")).history(start=start.strftime("%Y-%m-%d"))
    if history.empty:
        raise RuntimeError(f"No price data returned for {symbol}")

    # Drop today's unfinished bar so signals are based on finished closes.
    # Crypto days run midnight to midnight UTC; stock days follow New York.
    today = datetime.now(timezone.utc if is_crypto(symbol) else MARKET_TZ).date()
    closes = history["Close"]
    closes = closes[closes.index.date < today]
    return closes.tail(days)


def get_client():
    load_dotenv(BOT_DIR / ".env")
    api_key = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    if not api_key or not secret_key or "your_" in api_key:
        log.error("Missing Alpaca keys. Fill in ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        sys.exit(1)

    return TradingClient(api_key, secret_key, paper=paper)


def market_open_today(client):
    """True if today is a stock trading day and the market hasn't closed yet."""
    now = datetime.now(MARKET_TZ)
    days = client.get_calendar(GetCalendarRequest(start=now.date(), end=now.date()))
    if not days:
        log.info("Stock market is closed today (weekend or holiday).")
        journal("Stocks: no trades  -  market closed today (weekend or holiday)")
        return False
    open_time = days[0].open.replace(tzinfo=MARKET_TZ)
    close = days[0].close.replace(tzinfo=MARKET_TZ)
    # A few minutes' grace so a slightly fast Mac clock doesn't skip the day.
    if now < open_time - timedelta(minutes=10):
        log.info(f"Stock market doesn't open until {open_time:%H:%M} ET.")
        journal(f"Stocks: waiting  -  market opens at {open_time:%I:%M %p} ET, stocks trade on the next run")
        return False
    if now >= close:
        log.info(f"Stock market already closed at {close:%H:%M} ET today.")
        journal("Stocks: no trades  -  bot ran after the market closed (was the Mac asleep?)", alert="warning")
        return False
    return True


def shares_held(client, symbol):
    try:
        # Alpaca lists crypto positions without the slash (BTCUSD).
        return float(client.get_open_position(symbol.replace("/", "")).qty)
    except APIError:
        return 0.0


def place_order(client, symbol, side, qty=None, notional=None, seconds=30):
    """Submit a market order and wait briefly for it to fill. Returns the order.

    Pass `qty` for a number of shares/coins or `notional` for a dollar amount.
    """
    order = client.submit_order(
        MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            notional=notional,
            side=side,
            # Crypto orders can't be day orders; good-til-canceled is the norm.
            time_in_force=TimeInForce.GTC if is_crypto(symbol) else TimeInForce.DAY,
        )
    )
    log.info(f"Order submitted: {side.value} {qty or '$' + str(notional)} {symbol} (id {order.id})")
    for _ in range(seconds):
        if order.status in DONE_STATUSES:
            break
        time.sleep(1)
        order = client.get_order_by_id(order.id)
    log.info(f"Order {order.id}: {order.status.value}, filled {order.filled_qty} at {order.filled_avg_price}")
    return order


def buy(client, symbol, key, name, price, positions):
    if is_crypto(symbol):
        # Crypto can be bought in fractions, so spend the exact dollar amount.
        dollars = config.CRYPTO_TRADE_DOLLARS
        qty = round(dollars / price, 6)
        order = place_order(client, symbol, OrderSide.BUY, notional=dollars)
    else:
        dollars = config.TRADE_DOLLARS
        qty = int(dollars // price)
        if qty < 1:
            log.warning(f"{name}: ${dollars:,} buys less than one share at {price:.2f}")
            return
        order = place_order(client, symbol, OrderSide.BUY, qty=qty)

    if order.status in FAILED_STATUSES:
        journal(f"{name}: BUY {symbol} FAILED ({order.status.value})", alert="error")
        return
    if order.status == OrderStatus.FILLED:
        record_buy(name, symbol, order, positions, key)
    else:
        # Still filling (common in the first minutes after the open). Hold the
        # spot with an estimate; settle_pending() records the real fill.
        positions[key] = {"shares": qty, "cost": qty * price, "order_id": str(order.id), "pending": "buy"}
        log.info(f"{name}: buy still filling, will check again")


def record_buy(name, symbol, order, positions, key):
    filled = float(order.filled_qty)
    fill_price = float(order.filled_avg_price)
    positions[key] = {"shares": filled, "cost": filled * fill_price, "order_id": str(order.id)}
    journal(f"{name}: BUY {filled:g} {symbol} at ${fill_price:,.2f} (total ${filled * fill_price:,.2f})", alert="buy")


def sell(client, symbol, key, name, positions):
    held = positions[key]
    # Never sell more than the account actually holds. (Alpaca takes crypto
    # fees out of the coins bought, so the account can hold slightly less.)
    qty = min(held["shares"], shares_held(client, symbol))
    if qty <= 0:
        log.warning(f"{name}: expected {held['shares']:g} but the account holds none")
        positions.pop(key)
        return
    order = place_order(client, symbol, OrderSide.SELL, qty=qty)
    if order.status in FAILED_STATUSES:
        journal(f"{name}: SELL {qty:g} {symbol} FAILED ({order.status.value})", alert="error")
        return
    if order.status == OrderStatus.FILLED:
        record_sell(name, symbol, order, positions, key)
    else:
        held.update(order_id=str(order.id), pending="sell")
        log.info(f"{name}: sell still filling, will check again")


def record_sell(name, symbol, order, positions, key):
    """Close out (or, for a partial fill, shrink) a strategy's position."""
    held = positions[key]
    sold = float(order.filled_qty)
    fill_price = float(order.filled_avg_price)
    proceeds = sold * fill_price
    ordered = float(order.qty)
    if sold >= ordered:
        positions.pop(key)
        cost = held["cost"]
    else:
        # Partly sold before the order expired: keep the rest.
        cost = held["cost"] * sold / held["shares"]
        held.update(shares=held["shares"] - sold, cost=held["cost"] - cost)
        held.pop("pending", None)
    profit = proceeds - cost
    journal(
        f"{name}: SELL {sold:g} {symbol} at ${fill_price:,.2f} (total ${proceeds:,.2f})"
        f"  -  {'PROFIT' if profit >= 0 else 'LOSS'} ${abs(profit):,.2f} ({profit / cost:+.1%})"
        + ("" if sold >= ordered else f"  -  only {sold:g} of {ordered:g} sold"),
        alert="profit" if profit >= 0 else "loss",
    )


def settle_pending(client, positions, wait_seconds=0):
    """Record the real fills of orders that were still working when placed.

    Waits up to `wait_seconds` for them to finish, checking every 10 seconds.
    """
    deadline = time.time() + wait_seconds
    while True:
        pending = [(symbol, key, held) for symbol, by_strategy in positions.items()
                   for key, held in list(by_strategy.items()) if held.get("pending")]
        for symbol, key, held in pending:
            order = client.get_order_by_id(held["order_id"])
            if order.status not in DONE_STATUSES:
                continue
            name = f"{symbol} {STRATEGIES[key][0]}"
            filled = float(order.filled_qty or 0)
            if held["pending"] == "buy":
                if filled:
                    record_buy(name, symbol, order, positions[symbol], key)
                else:
                    positions[symbol].pop(key)
                    journal(f"{name}: BUY {symbol} FAILED ({order.status.value})", alert="error")
            else:
                if filled:
                    record_sell(name, symbol, order, positions[symbol], key)
                else:
                    held.pop("pending")
                    journal(f"{name}: SELL {symbol} FAILED ({order.status.value}), still holding", alert="error")
            save_positions({s: h for s, h in positions.items() if h})

        still_pending = sum(1 for by_strategy in positions.values() for held in by_strategy.values() if held.get("pending"))
        if not still_pending or time.time() >= deadline:
            if still_pending:
                log.info(f"{still_pending} orders still filling; they'll be recorded on the next run")
            return
        time.sleep(10)


def run_symbol(client, symbol, strategy_keys, positions, dry_run):
    """Run the given strategies on one symbol. Returns True if any wanted to trade."""
    closes = fetch_prices(symbol, config.LOOKBACK_DAYS)
    price = closes.iloc[-1]
    log.info(f"{symbol}: fetched {len(closes)} days of prices, latest close {price:,.2f} on {closes.index[-1].date()}")

    today = datetime.now(timezone.utc if is_crypto(symbol) else MARKET_TZ).date()
    held_by_strategy = positions.setdefault(symbol, {})
    traded = False

    for key in strategy_keys:
        strategy_name, strategy = STRATEGIES[key]
        name = f"{symbol} {strategy_name}"
        want = strategy(closes, next_day=today).iloc[-1]
        holding = key in held_by_strategy
        if holding and held_by_strategy[key].get("pending"):
            log.info(f"{name}: waiting for an earlier order to finish filling")
            continue
        log.info(f"{name}: {describe(key, closes)} -> {'hold' if want else 'cash'}"
                 f" (currently {'holding ' + format(held_by_strategy[key]['shares'], 'g') if holding else 'in cash'})")

        if want and not holding:
            action = "buy"
        elif not want and holding:
            action = "sell"
        else:
            continue

        traded = True
        if dry_run:
            log.info(f"{name}: would {action} (dry run, no order placed)")
        elif action == "buy":
            buy(client, symbol, key, name, price, held_by_strategy)
        else:
            sell(client, symbol, key, name, held_by_strategy)
        if not dry_run:
            save_positions({s: held for s, held in positions.items() if held})
    return traded


def run_group(client, label, symbols, strategy_keys, positions, dry_run):
    traded = False
    for symbol in symbols:
        try:
            traded |= run_symbol(client, symbol, strategy_keys, positions, dry_run)
        except Exception:
            # One symbol failing (bad data, rejected order) shouldn't stop the others.
            log.exception(f"{symbol}: run failed")
            if not dry_run:
                journal(f"ERROR  -  {symbol} failed, see logs/bot.log", alert="error")
    if not traded:
        log.info(f"{label}: no strategy wants to trade.")
        if not dry_run:
            journal(f"{label}: no trades  -  no strategy signaled a change")


def run(dry_run):
    log.info(f"Running on {', '.join(config.SYMBOLS + config.CRYPTO_SYMBOLS)}{' (dry run)' if dry_run else ''}")

    client = None if dry_run else get_client()
    positions = load_positions()
    if not dry_run:
        # Pick up fills from orders an earlier run left still working.
        settle_pending(client, positions)

    if config.SYMBOLS:
        if dry_run or market_open_today(client):
            run_group(client, "Stocks", config.SYMBOLS, config.ENABLED_STRATEGIES, positions, dry_run)
        else:
            log.info("Skipping stocks today.")

    if config.CRYPTO_SYMBOLS:
        run_group(client, "Crypto", config.CRYPTO_SYMBOLS, config.CRYPTO_STRATEGIES, positions, dry_run)

    if not dry_run:
        # Opening orders can take a few minutes to fill; wait so the journal
        # and Discord get real prices.
        settle_pending(client, positions, wait_seconds=600)
        send_summary(client)


def previous_close_equity(client, account):
    """Account value at the last close. Alpaca sometimes reports 0 (e.g. for a
    brand-new account), so fall back to its portfolio history, then the start."""
    last_equity = float(account.last_equity)
    if last_equity > 0:
        return last_equity
    from alpaca.trading.requests import GetPortfolioHistoryRequest
    history = client.get_portfolio_history(GetPortfolioHistoryRequest(period="1W", timeframe="1D"))
    earlier = [e for e in (history.equity or []) if e]
    return earlier[-1] if earlier else float(config.STARTING_BALANCE)


def send_summary(client):
    """One Discord message per run: account value plus everything that happened."""
    account = client.get_account()
    equity, last_equity = float(account.equity), previous_close_equity(client, account)
    change = equity - last_equity
    lines = [
        f"**Account value:** ${equity:,.2f} ({'+' if change >= 0 else '-'}${abs(change):,.2f} today)",
        f"**Cash:** ${float(account.cash):,.2f}  |  **Open positions:** {len(client.get_all_positions())}",
        "",
    ] + [f"- {line}" for line in run_lines]
    notify.send(f"Daily run - {datetime.now(MARKET_TZ):%a %b %d, %I:%M %p} ET", "\n".join(lines),
                kind="loss" if change < 0 else "profit")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="show the signals without placing orders")
    parser.add_argument("--test-discord", action="store_true", help="send a test message to Discord and exit")
    args = parser.parse_args()

    load_dotenv(BOT_DIR / ".env")
    setup_logging()
    if args.test_discord:
        ok = notify.send("Trading bot connected", "Alerts for trades, profits, losses and errors will show up here.", kind="profit")
        print("Sent! Check your Discord channel." if ok else "Not sent. Check DISCORD_WEBHOOK_URL in .env (see logs/bot.log).")
        return
    try:
        run(args.dry_run)
    except Exception:
        log.exception("Bot run failed")
        journal("ERROR  -  the bot crashed, see logs/bot.log", alert="error")
        sys.exit(1)
    finally:
        log.info("-" * 60)


if __name__ == "__main__":
    main()
