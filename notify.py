"""Send bot alerts to a Discord channel through a webhook.

Set DISCORD_WEBHOOK_URL in .env. If it's missing, alerts are skipped.
A failed alert is logged but never stops the bot.
"""

import json
import logging
import os
import ssl
import urllib.request

import certifi

log = logging.getLogger("bot")

# Python from python.org on macOS ships without trusted certificates.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

GREEN = 0x2ECC71
RED = 0xE74C3C
ORANGE = 0xE67E22
BLUE = 0x3498DB
GRAY = 0x95A5A6

COLORS = {"buy": BLUE, "profit": GREEN, "loss": RED, "error": RED, "warning": ORANGE, "info": GRAY}


def send(title, description="", kind="info", fields=None):
    """Post one message. `fields` is an optional list of (name, value, inline)."""
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url or "your_" in url:
        return False
    embed = {"title": title[:256], "description": description[:4000], "color": COLORS.get(kind, GRAY)}
    if fields:
        embed["fields"] = [{"name": n[:256], "value": (v or "-")[:1024], "inline": inline} for n, v, inline in fields[:25]]
    payload = {"embeds": [embed]}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        # Discord rejects Python's default user agent.
        headers={"Content-Type": "application/json", "User-Agent": "TradingBot/1.0"},
    )
    try:
        urllib.request.urlopen(request, timeout=10, context=SSL_CONTEXT)
        return True
    except Exception as e:
        log.warning(f"Discord alert failed: {e}")
        return False
