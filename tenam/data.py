"""Download 1-minute bars for QQQ and SPY once and cache them to parquet.

    python data.py           download what's missing, then print a summary
    python data.py --check   print the summary from the cache only

Bars are kept for regular hours only (09:30 to the day's close, using Alpaca's
calendar, so early closes end at 13:00). Timestamps are New York time.
"""

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

import config

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
NY = "America/New_York"

load_dotenv(HERE.parent / ".env")


def keys():
    key, secret = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not secret:
        sys.exit("Set ALPACA_API_KEY and ALPACA_SECRET_KEY in the bot's .env file.")
    return key, secret


def bars_path(symbol):
    return DATA_DIR / f"{symbol}_1min.parquet"


def calendar_path():
    return DATA_DIR / "calendar.parquet"


def download_calendar(end):
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import GetCalendarRequest

    client = TradingClient(*keys(), paper=True)
    days = client.get_calendar(GetCalendarRequest(start=config.DATA_START, end=end))
    cal = pd.DataFrame(
        {
            "date": [pd.Timestamp(d.date) for d in days],
            "open": [pd.Timestamp(d.open).tz_localize(NY) for d in days],
            "close": [pd.Timestamp(d.close).tz_localize(NY) for d in days],
        }
    )
    cal.to_parquet(calendar_path(), index=False)
    return cal


def load_calendar():
    return pd.read_parquet(calendar_path())


def fetch(client, symbol, start, end, feed):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=DataFeed(feed),
        adjustment=Adjustment.RAW,  # intraday R doesn't need dividend adjustment
    )
    df = client.get_stock_bars(req).df
    if df.empty:
        return df
    df = df.reset_index()
    df["timestamp"] = df["timestamp"].dt.tz_convert(NY)
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def regular_hours(df, cal):
    """Keep bars from the day's open up to (not including) its close."""
    df = df.copy()
    df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()
    df = df.merge(cal.rename(columns={"open": "mkt_open", "close": "mkt_close"}), on="date")
    keep = (df["timestamp"] >= df["mkt_open"]) & (df["timestamp"] < df["mkt_close"])
    return df.loc[keep, ["timestamp", "date", "open", "high", "low", "close", "volume"]]


def download(symbol, cal, feed, through=None):
    """Fetch missing days up to `through` (default yesterday)."""
    from alpaca.data.historical import StockHistoricalDataClient

    client = StockHistoricalDataClient(*keys())
    path = bars_path(symbol)
    have = pd.read_parquet(path) if path.exists() else None
    start = config.DATA_START if have is None else (have["date"].max() + timedelta(days=1)).date()
    # The free plan serves SIP history only up to 15 minutes ago.
    last = through or date.today() - timedelta(days=1)
    now_ny = datetime.now(ZoneInfo(NY))
    if last >= now_ny.date() and now_ny.hour * 60 + now_ny.minute < 16 * 60 + 20:
        last = now_ny.date() - timedelta(days=1)  # never cache a day that isn't over
    latest = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=16)
    chunks = []
    while start <= last:
        stop = min(date(start.year, 12, 31), last)
        print(f"  {symbol} {start} to {stop} ({feed})", flush=True)
        part = fetch(
            client,
            symbol,
            datetime.combine(start, datetime.min.time()),
            min(datetime.combine(stop + timedelta(days=1), datetime.min.time()), latest),
            feed,
        )
        if not part.empty:
            chunks.append(regular_hours(part, cal))
        start = stop + timedelta(days=1)
    if not chunks:
        return have
    new = pd.concat(chunks, ignore_index=True)
    out = new if have is None else pd.concat([have, new], ignore_index=True)
    out = out.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True)
    out.to_parquet(path, index=False)
    return out


def load_bars(symbol):
    return pd.read_parquet(bars_path(symbol))


def summary(symbol, cal):
    df = load_bars(symbol)
    first, last = df["date"].min(), df["date"].max()
    days = cal[(cal["date"] >= first) & (cal["date"] <= last)].copy()
    days["expected"] = ((days["close"] - days["open"]).dt.total_seconds() // 60).astype(int)
    got = df.groupby("date").size().rename("bars")
    days = days.merge(got, left_on="date", right_index=True, how="left").fillna({"bars": 0})
    days["year"] = days["date"].dt.year
    print(f"\n{symbol}: {len(df):,} bars, {first.date()} to {last.date()}")
    table = days.groupby("year").agg(
        trading_days=("date", "size"),
        days_with_bars=("bars", lambda b: int((b > 0).sum())),
        bars=("bars", "sum"),
        expected_bars=("expected", "sum"),
    )
    table["bars"] = table["bars"].astype(int)
    table["coverage"] = (table["bars"] / table["expected_bars"]).map("{:.2%}".format)
    print(table.to_string())
    missing = days[days["bars"] == 0]["date"].dt.date.tolist()
    print(f"Missing trading days: {missing if missing else 'none'}")
    thin = days[(days["bars"] > 0) & (days["bars"] < days["expected"] * 0.95)]
    if len(thin):
        print(f"Days with under 95% of their minute bars: {len(thin)}")
        print(thin[["date", "bars", "expected"]].head(10).to_string(index=False))
    short = days[days["expected"] < 390]["date"].dt.date.tolist()
    print(f"Early-close days in range: {len(short)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    DATA_DIR.mkdir(exist_ok=True)
    if args.check:
        cal = load_calendar()
    else:
        cal = download_calendar(date.today())
        feed = config.FEED
        for symbol in config.SYMBOLS:
            try:
                download(symbol, cal, feed)
            except Exception as exc:  # the free plan may refuse recent SIP data
                if feed == "sip" and "subscription" in str(exc).lower():
                    print(f"SIP refused ({exc}); using IEX. IEX prices and volume are thinner.")
                    feed = "iex"
                    download(symbol, cal, feed)
                else:
                    raise
        (DATA_DIR / "feed.txt").write_text(feed)
    print(f"Feed: {(DATA_DIR / 'feed.txt').read_text()}")
    for symbol in config.SYMBOLS:
        summary(symbol, cal)


if __name__ == "__main__":
    main()
