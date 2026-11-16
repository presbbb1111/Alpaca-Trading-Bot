"""Download and cache the bars the three sleeves need (SIP, regular hours).

    python data.py            download what's missing
    python data.py --check    print what's cached

- daily bars for the dip-buying list, split-adjusted
- 1-minute bars for the grid's ETF
- 5-minute bars for the intraday list
Timestamps are New York time. Cached in buylow/data/ (gitignored).
"""

import argparse
import os
import sys
import time
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


def calendar(refresh=False):
    path = DATA_DIR / "calendar.parquet"
    if refresh or not path.exists():
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetCalendarRequest

        days = TradingClient(*keys(), paper=True).get_calendar(
            GetCalendarRequest(start=config.DATA_START, end=date.today() + timedelta(days=30)))
        cal = pd.DataFrame({"date": [pd.Timestamp(d.date) for d in days],
                            "open": [pd.Timestamp(d.open).tz_localize(NY) for d in days],
                            "close": [pd.Timestamp(d.close).tz_localize(NY) for d in days]})
        DATA_DIR.mkdir(exist_ok=True)
        cal.to_parquet(path, index=False)
    return pd.read_parquet(path)


def path_for(kind, symbol):
    return DATA_DIR / kind / f"{symbol.replace('.', '_')}.parquet"


def last_complete_day():
    """Yesterday, or today once it's 16:20 ET (SIP is served 15+ minutes late)."""
    now = datetime.now(ZoneInfo(NY))
    return now.date() if now.hour * 60 + now.minute >= 16 * 60 + 20 else now.date() - timedelta(days=1)


def fetch(symbols, start, end, minutes, adjustment):
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    tf = TimeFrame.Day if minutes is None else TimeFrame(minutes, TimeFrameUnit.Minute)
    client = StockHistoricalDataClient(*keys())
    latest = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=16)
    for attempt in range(5):
        try:
            req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=tf, feed=DataFeed.SIP,
                                   adjustment=Adjustment(adjustment), start=start,
                                   end=min(datetime.combine(end + timedelta(days=1), datetime.min.time()), latest))
            df = client.get_stock_bars(req).df
            break
        except Exception as e:  # rate limits and dropped connections: wait and retry
            if attempt == 4:
                raise
            print(f"    retrying after: {str(e)[:120]}", flush=True)
            time.sleep(10 * (attempt + 1))
    if df.empty:
        return df
    df = df.reset_index()
    df["timestamp"] = df["timestamp"].dt.tz_convert(NY)
    return df[["symbol", "timestamp", "open", "high", "low", "close", "volume"]]


def regular_hours(df, cal):
    df = df.copy()
    df["date"] = df["timestamp"].dt.tz_localize(None).dt.normalize()
    df = df.merge(cal.rename(columns={"open": "mkt_open", "close": "mkt_close"}), on="date")
    keep = (df["timestamp"] >= df["mkt_open"]) & (df["timestamp"] < df["mkt_close"])
    return df.loc[keep, ["timestamp", "date", "open", "high", "low", "close", "volume"]]


def download_daily(symbols):
    """Split-adjusted daily bars, re-downloaded whole each time (a split
    changes every earlier price)."""
    last = last_complete_day()
    out = DATA_DIR / "daily"
    out.mkdir(parents=True, exist_ok=True)
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i + 20]
        print(f"  daily {', '.join(batch)}", flush=True)
        df = fetch(batch, datetime.combine(config.DATA_START, datetime.min.time()), last, None, "split")
        for s, g in df.groupby("symbol"):
            g = g.assign(date=g["timestamp"].dt.tz_localize(None).dt.normalize())
            g[["date", "open", "high", "low", "close", "volume"]].to_parquet(path_for("daily", s), index=False)


def download_intraday(symbols, minutes, cal):
    """Raw intraday bars, adding only the days that are missing."""
    kind = f"{minutes}min"
    (DATA_DIR / kind).mkdir(parents=True, exist_ok=True)
    last = last_complete_day()
    for s in symbols:
        path = path_for(kind, s)
        have = pd.read_parquet(path) if path.exists() else None
        start = config.DATA_START if have is None else (have["date"].max() + timedelta(days=1)).date()
        chunks = []
        while start <= last:
            stop = min(date(start.year, 12, 31), last)
            print(f"  {kind} {s} {start} to {stop}", flush=True)
            part = fetch(s, datetime.combine(start, datetime.min.time()), stop, minutes, "raw")
            if not part.empty:
                chunks.append(regular_hours(part, cal))
            start = stop + timedelta(days=1)
        if chunks:
            new = pd.concat(chunks, ignore_index=True)
            df = new if have is None else pd.concat([have, new], ignore_index=True)
            df.drop_duplicates("timestamp").sort_values("timestamp").reset_index(drop=True).to_parquet(path, index=False)


def load_daily(symbol):
    return pd.read_parquet(path_for("daily", symbol))


def load_intraday(symbol, minutes):
    return pd.read_parquet(path_for(f"{minutes}min", symbol))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        cal = calendar(refresh=True)
        download_daily(config.DIP_UNIVERSE)
        download_intraday([config.GRID_SYMBOL], 1, cal)
        download_intraday(config.STRETCH_UNIVERSE, config.STRETCH_BAR_MINUTES, cal)
    for kind in ("daily", "1min", f"{config.STRETCH_BAR_MINUTES}min"):
        files = sorted((DATA_DIR / kind).glob("*.parquet"))
        if not files:
            continue
        spans = [(f.stem, pd.read_parquet(f, columns=["date"])["date"]) for f in files]
        print(f"{kind}: {len(files)} symbols, "
              f"{min(d.min() for _, d in spans).date()} to {max(d.max() for _, d in spans).date()}, "
              f"{sum(len(d) for _, d in spans):,} bars")
        late = [s for s, d in spans if d.min().date() > config.DEV_START]
        if late:
            print(f"  starting after {config.DEV_START}: {', '.join(late)}")


if __name__ == "__main__":
    main()
