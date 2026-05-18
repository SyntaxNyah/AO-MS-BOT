"""Configuration loaded from the environment / .env file."""
import os
import re
from urllib.parse import quote

from dotenv import load_dotenv

load_dotenv()


def _int(name, default):
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


def _str(name, default):
    return (os.getenv(name) or "").strip() or default


def _bool(name, default):
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on", "y")


def _list(name, default):
    """Parse a comma/newline-separated list, dropping blanks and duplicates."""
    raw = os.getenv(name) or ""
    seen, out = set(), []
    for item in re.split(r"[,\n]", raw):
        item = item.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out or default


# --- Discord ---
DISCORD_TOKEN = _str("DISCORD_TOKEN", "")
GUILD_ID = _int("GUILD_ID", 0)
EVENTS_CHANNEL_ID = _int("EVENTS_CHANNEL_ID", 0)
INTEGRITY_CHANNEL_ID = _int("INTEGRITY_CHANNEL_ID", 0)

# --- Master server polling ---
# One or more master-server list endpoints. Set MS_URL to a single URL, or to
# several URLs separated by commas (or newlines) to poll multiple masters at
# once -- their server lists are merged, deduplicated by ip:port.
MS_URLS = _list("MS_URL", [
    "https://servers.aceattorneyonline.com/servers",
    "https://servers.umineko.online/servers",
])
# First configured master; kept for callers/displays that expect a single URL.
MS_URL = MS_URLS[0]
POLL_INTERVAL_MINUTES = _int("POLL_INTERVAL_MINUTES", 1)

# --- Dead-server detection ---
# A server absent from the master list for at least this many days is treated
# as shut down and surfaces in /deadservers. Stored history is never deleted,
# so a server that re-pings the list updates its last_seen and drops off the
# dead list again automatically.
DEAD_SERVER_DAYS = _int("DEAD_SERVER_DAYS", 60)

# --- Storage ---
DATA_DIR = _str("DATA_DIR", "data")
DB_PATH = os.path.join(DATA_DIR, "ao_monitor.db")
EVENTS_LOG = os.path.join(DATA_DIR, "events.log")
BOT_LOG = os.path.join(DATA_DIR, "bot.log")

# --- Heartbeat-counter analysis tuning ---
# Servers report an ever-rising hbcounter. It climbs ~1 per minute, and when it
# reaches HB_CAP it rolls over and continues from (HB_CAP - ROLLOVER_DROP).
HB_CAP = 50000          # counter resets when it reaches this value
ROLLOVER_DROP = 1000    # 50000 -> 49000
HB_RATE_MAX = 2.0       # most plausible counter gain per minute (true rate ~1/min)
HB_MARGIN = 12          # absolute slack on top of the rate-based expectation
# An upward jump is far less alarming than a backwards drop. The master server
# only publishes the counter every few minutes, so when it refreshes the
# counter leaps by all the minutes it accumulated meanwhile -- a +20-30 step is
# routine, not tampering. Allow this much slack before an upward jump is even
# noted (and it is never raised to a high-severity alert).
HB_JUMP_MARGIN = 35
RELIABLE_GAP_FACTOR = 3  # a poll gap below interval*this is "reliable" for alerting
# The master server keeps a dead server listed for ~30 min after its last
# heartbeat. A server taken down and brought back starts its counter from
# scratch, so a counter that has only had time to climb this far after a drop
# is an ordinary restart -- not a fault. A few counts of headroom above the
# 30-min window keep borderline restarts from being mistaken for tampering.
HB_RESTART_WINDOW = 35
# Minimum minutes since the last reading for a counter that has slammed to the
# floor to count as a genuine restart. A real down-and-back cycle takes time
# (the master list holds a dead entry ~30 min); a counter hitting the floor
# faster than this never had time to actually restart -- likely a manual reset.
HB_REAL_RESTART_MINUTES = 35

# --- Player-count / bot-pattern analysis ---
# A server that normally sits near-empty suddenly filling with players over a
# poll or two looks like an automated bot fill, not organic traffic. A jump to
# at least BOT_SPIKE_MIN players, within BOT_SPIKE_POLLS polls, on a server
# whose baseline is at or below BOT_BASELINE_MAX, is flagged as a bot pattern.
BOT_SPIKE_MIN = 40        # players: low end of a suspicious sudden burst
BOT_SPIKE_MAX = 100       # players: high end of the typical bot-fill band
BOT_BASELINE_MAX = 8      # a server averaging at/under this is "normally empty"
BOT_SPIKE_POLLS = 2       # the burst must appear within this many polls
BOT_BASELINE_WINDOW = 30  # how many recent snapshots define the baseline

# --- Website / live dashboard ---
# Set WEBSITE_ENABLED=1 to also serve a live web dashboard alongside the bot.
# It runs in the same process, reads the same database, and shows every
# server, the global player count, anomalies and per-server history -- so the
# bot's whole world is browsable in a web page, no Discord needed.
WEBSITE_ENABLED = _bool("WEBSITE_ENABLED", False)
WEBSITE_HOST = _str("WEBSITE_HOST", "0.0.0.0")
WEBSITE_PORT = _int("WEBSITE_PORT", 8080)
# Shown in the dashboard header -- name it whatever you like.
WEBSITE_TITLE = _str("WEBSITE_TITLE", "SyntaxNyah AO Dashboard")

# --- WebAO join links ---
# WebAO is the browser client for Attorney Online. A server that publishes a
# websocket port can be joined straight from a browser, so the bot and the
# dashboard offer a one-click "join in browser" link for every such server.
# The scheme is stored without http(s):// -- webao_url() picks the right one.
WEBAO_CLIENT = _str("WEBAO_CLIENT", "webao.miku.pizza/client.html")


def webao_url(ip, ws_port=None, wss_port=None, name=""):
    """Build a WebAO 'join this server' link, or None if it has no WS port.

    A secure websocket (wss) is preferred and the WebAO client is then loaded
    over https; a plain ws server loads the client over http. This matches
    WebAO's own http/https redirect, avoiding a needless extra hop.
    """
    base = re.sub(r"^https?://", "", WEBAO_CLIENT)
    if wss_port:
        scheme, endpoint = "https", f"wss://{ip}:{wss_port}"
    elif ws_port:
        scheme, endpoint = "http", f"ws://{ip}:{ws_port}"
    else:
        return None
    return (f"{scheme}://{base}?mode=join&connect={endpoint}"
            f"&serverName={quote(name or '', safe='')}")
