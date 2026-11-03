"""Daily review: gather the day's facts, and post the written review to Discord.

The review itself (what went right, what went wrong, proposed changes) is
written after the end-of-day summary. This script does the two mechanical parts:

    python review.py data                       # today's facts, as text
    python review.py data --date 2026-11-03
    python review.py post reviews/2026-11-03.md # post a written review to Discord
    python review.py changes                    # post the latest commit to Discord
    python review.py changes --since 2026-11-03 # post every commit since a date

Proposed changes are tracked in reviews/proposals.md. Each one waits for an
approve or deny before anything changes.
"""

import argparse
import re
import subprocess
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from alpaca.trading.enums import QueryOrderStatus
from alpaca.trading.requests import GetOrdersRequest
from dotenv import load_dotenv

import notify
import summary
from bot import get_client

BOT_DIR = Path(__file__).resolve().parent
REVIEWS = BOT_DIR / "reviews"
PROPOSALS = REVIEWS / "proposals.md"
ET = ZoneInfo("America/New_York")
LOGS = ["bot.log", "orb.log", "bot.err.log", "orb.err.log", "summary.err.log", "dashboard.err.log"]


def section(title, body):
    return f"## {title}\n{body.strip() or '(nothing)'}\n"


def journal_day(path, day, header):
    """The lines under one day's heading in trades.txt or orb_trades.txt."""
    if not path.exists():
        return ""
    lines, inside = [], False
    for line in path.read_text().splitlines():
        if line.startswith("====="):
            inside = line == header
            continue
        if inside and line.strip():
            lines.append(line.strip())
    return "\n".join(lines)


def log_problems(day):
    """WARNING and ERROR lines from the logs for one day, without tracebacks."""
    out = []
    for name in LOGS:
        path = BOT_DIR / "logs" / name
        if not path.exists():
            continue
        hits = [l for l in path.read_text(errors="replace").splitlines()
                if l.startswith(day.isoformat()) and (" WARNING " in l or " ERROR " in l)]
        # Collapse repeats, which happen when a loop keeps failing.
        counts = {}
        for l in hits:
            key = re.sub(r"^\S+ \S+ ", "", l)[:200]
            counts[key] = counts.get(key, 0) + 1
        out += [f"{name}: {k}" + (f"  (x{n})" if n > 1 else "") for k, n in counts.items()]
    return "\n".join(out)


def orb_timeline(client, day):
    """Each ORB trade's entry and exit times from Alpaca, and how long it was held."""
    orders = client.get_orders(GetOrdersRequest(
        status=QueryOrderStatus.ALL, after=datetime.combine(day, dtime(9, 0), ET),
        until=datetime.combine(day + timedelta(days=1), dtime(0, 0), ET), limit=500))
    by_symbol = {}
    for o in orders:
        cid = o.client_order_id or ""
        if cid.startswith(f"orb-{day:%Y%m%d}-") and o.filled_at:
            symbol, kind = cid.split("-", 3)[2:4]
            by_symbol.setdefault(symbol, []).append((o.filled_at.astimezone(ET), kind, float(o.filled_avg_price)))
    rows = []
    for symbol, fills in sorted(by_symbol.items()):
        fills.sort()
        entry, exits = fills[0], fills[1:]
        if not exits:
            rows.append(f"{symbol}: in {entry[0]:%H:%M:%S} ({entry[1]}), no exit order filled")
            continue
        out = exits[-1]
        held = (out[0] - entry[0]).total_seconds()
        rows.append(f"{symbol}: in {entry[0]:%H:%M:%S} ({entry[1]}) at {entry[2]:.2f}, "
                    f"out {out[0]:%H:%M:%S} ({out[1]}) at {out[2]:.2f}, held {held / 60:.1f} min")
    return "\n".join(rows)


def git_state():
    run = lambda *a: subprocess.run(["git", *a], cwd=BOT_DIR, capture_output=True, text=True).stdout.strip()
    return (f"Branch: {run('status', '-sb').splitlines()[0] if run('status', '-sb') else '?'}\n"
            f"Uncommitted files:\n{run('status', '--short') or '(none)'}\n"
            f"Recent commits:\n{run('log', '--oneline', '-5')}")


def data(day):
    load_dotenv(BOT_DIR / ".env")
    client = get_client()
    header = f"===== {day:%A, %B} {day.day}, {day.year} ====="
    orb_header = f"===== ORB {day:%A, %B} {day.day}, {day.year} ====="
    parts = [f"# Review data for {day:%A, %B} {day.day}, {day.year}\n"]
    if day == date.today():
        _, overview, _, fields = summary.build(client)
        parts.append(section("End-of-day summary", "\n\n".join([overview] + [f"[{n}]\n{v}" for n, v, _ in fields])))
    parts.append(section("Daily bot journal", journal_day(BOT_DIR / "trades.txt", day, header)))
    parts.append(section("ORB journal", journal_day(BOT_DIR / "orb_trades.txt", day, orb_header)))
    fills = BOT_DIR / "orb_fills.csv"
    if fills.exists():
        rows = fills.read_text().splitlines()
        parts.append(section("ORB fills (orb_fills.csv)", "\n".join([rows[0]] + [r for r in rows[1:] if r.startswith(day.isoformat())])))
    parts.append(section("ORB timeline from Alpaca", orb_timeline(client, day)))
    parts.append(section("Warnings and errors in the logs", log_problems(day)))
    parts.append(section("Git", git_state()))
    parts.append(section("Proposals so far (reviews/proposals.md)", PROPOSALS.read_text() if PROPOSALS.exists() else ""))
    return "\n".join(parts)


def chunks(text, limit=3900):
    """Split text into pieces under Discord's limit, breaking at line ends."""
    piece = ""
    for line in text.splitlines(keepends=True):
        if len(piece) + len(line) > limit and piece:
            yield piece
            piece = ""
        piece += line
    if piece.strip():
        yield piece


def post(path):
    """Post a review to Discord: one message per '## ' section."""
    load_dotenv(BOT_DIR / ".env")
    text = Path(path).read_text()
    title = re.search(r"^# (.+)$", text, re.M)
    title = title.group(1) if title else Path(path).stem
    sections = re.split(r"^## ", text, flags=re.M)[1:] or [text]
    notify.send(f"📋 {title}", "", "info")
    for s in sections:
        heading, _, body = s.partition("\n")
        kind = "error" if re.search(r"wrong|fix", heading, re.I) else "warning" if re.search(r"approv|propos", heading, re.I) else "info"
        for i, piece in enumerate(chunks(body.strip())):
            notify.send(heading.strip() + (" (cont.)" if i else ""), piece, kind)


def changes(since=None):
    """Post commits to Discord as "Change made" messages. The git post-commit
    hook runs this after every commit, so every change shows up there."""
    load_dotenv(BOT_DIR / ".env")
    run = lambda *a: subprocess.run(["git", *a], cwd=BOT_DIR, capture_output=True, text=True).stdout.strip()
    commits = run("log", "--reverse", "--format=%h", f"--since={since}").split() if since else [run("log", "-1", "--format=%h")]
    for h in commits:
        subject, _, body = run("log", "-1", "--format=%s%n%b", h).partition("\n")
        files = run("show", "--format=", "--name-only", h)
        notify.send(f"🔧 Change made: {subject}", f"{body.strip()}\n\n**Files:** {', '.join(files.split())}\n**Commit:** {h}", "buy")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    d = sub.add_parser("data", help="print the day's facts")
    d.add_argument("--date", type=date.fromisoformat, default=date.today())
    p = sub.add_parser("post", help="post a written review to Discord")
    p.add_argument("file")
    c = sub.add_parser("changes", help="post commits to Discord")
    c.add_argument("--since", help="post every commit since this date instead of just the latest")
    args = parser.parse_args()
    if args.command == "data":
        print(data(args.date))
    elif args.command == "post":
        post(args.file)
    else:
        changes(args.since)


if __name__ == "__main__":
    main()
