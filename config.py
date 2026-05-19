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
# Slack below the restart window. Poll timing jitter means a genuine restart
# can land a reading a little short of the window; without this margin a cycle
# that came back seconds early would be flagged as a manual reset. Only a
# reading this far inside the window counts as too-fast-to-be-real.
HB_RESET_EDGE_MARGIN = 1

# --- Per-master-server anomaly rules ---
# The bot polls more than one master server, and they do not all behave the
# same way -- so each master gets its own heartbeat-analysis profile and its
# anomalies are labelled with the master they came from. Alerts for the two
# masters therefore stay clearly separate even when they share a channel.
#
# Vanilla Attorney Online master: publishes each server's hbcounter only every
# few minutes, so the counter arrives in batchy +20-30 leaps and needs
# generous slack. It caps/rolls per HB_CAP / ROLLOVER_DROP above.
#
# Umineko Online master (Nyan-AO-Master-Server): clock-anchored. It advances
# each server's counter by exactly the whole minutes of master-verified uptime
# elapsed, so the counter climbs smoothly ~1/min and cannot be inflated by
# heartbeat flooding. It caps at 10080 and rolls over to 9000. Tighter
# tolerances apply, and its rollover model differs from vanilla.

# Which polled masters are the Umineko-style master. Any source URL listed
# here is analysed with the Umineko profile; every other master uses vanilla.
MS_UMINEKO_URLS = _list("MS_UMINEKO_URL", [
    "https://servers.umineko.online/servers",
])

VANILLA_HB_RULES = {
    "label": "Attorney Online",
    "hb_cap": HB_CAP,
    "rollover_drop": ROLLOVER_DROP,
    "hb_rate_max": HB_RATE_MAX,
    "hb_margin": HB_MARGIN,
    "hb_jump_margin": HB_JUMP_MARGIN,
    "hb_restart_window": HB_RESTART_WINDOW,
    "hb_real_restart_minutes": HB_REAL_RESTART_MINUTES,
    "hb_reset_edge_margin": HB_RESET_EDGE_MARGIN,
}

# A Umineko server can go quiet without dropping off the list: the master
# keeps a silent server listed for MS_UMINEKO_HEARTBEAT_EXPIRY_MINUTES (~60).
# The counter is only advanced when a heartbeat actually arrives, so when a
# quiet server resumes, its next heartbeat credits every elapsed real minute
# at once -- a single, legitimate leap of up to the whole expiry window, even
# though the bot's own poll gap is only a minute. Anomaly tolerances that
# depend on a single-step jump must clear that window or every resumed server
# would false-positive.
UMINEKO_HEARTBEAT_EXPIRY_MINUTES = _int(
    "MS_UMINEKO_HEARTBEAT_EXPIRY_MINUTES", 60)

UMINEKO_HB_RULES = {
    "label": "Umineko Online",
    "hb_cap": _int("MS_UMINEKO_HBCOUNTER_CAP", 10080),
    "rollover_drop": _int("MS_UMINEKO_HBCOUNTER_ROLLOVER_DROP", 1080),
    # Clock-anchored: the counter can never outpace real time, so a modest
    # per-minute rate with a little slack is all the rollover model needs.
    "hb_rate_max": 1.5,
    "hb_margin": 4,
    # A quiet-then-resumed server leaps by up to one whole expiry window in a
    # single heartbeat (see above), so the jump margin clears that plus slack.
    "hb_jump_margin": UMINEKO_HEARTBEAT_EXPIRY_MINUTES + 5,
    # Registration sets the counter to 1, so a restarted server reads near 0.
    "hb_restart_window": 10,
    # A genuine down-and-back cycle needs the server to actually drop off the
    # list first, which takes the full expiry window.
    "hb_real_restart_minutes": UMINEKO_HEARTBEAT_EXPIRY_MINUTES,
    "hb_reset_edge_margin": HB_RESET_EDGE_MARGIN,
}


def ms_rules(source_url):
    """Return the heartbeat-analysis rule profile for the master that listed a
    server. Umineko-style masters use the Umineko profile; every other master
    uses the vanilla Attorney Online profile."""
    if source_url and source_url in MS_UMINEKO_URLS:
        return UMINEKO_HB_RULES
    return VANILLA_HB_RULES


def ms_label(source_url):
    """Human-readable name of the master server a `source` URL refers to."""
    return ms_rules(source_url)["label"]


# --- Player-count / bot-pattern analysis ---
# A server that normally sits near-empty suddenly filling with players over a
# poll or two looks like an automated bot fill, not organic traffic. A jump to
# at least BOT_SPIKE_MIN players, within BOT_SPIKE_POLLS polls, on a server
# whose baseline is at or below BOT_BASELINE_MAX, is flagged as a bot pattern.
# Every threshold below is env-configurable so the detector can be retuned
# without a code change. The defaults below are what ships out of the box.
BOT_SPIKE_MIN = _int("BOT_SPIKE_MIN", 40)
# players: low end of a suspicious sudden burst.
BOT_SPIKE_MAX = _int("BOT_SPIKE_MAX", 100)
# players: high end of the typical bot-fill band; bursts above this still
# fire, the message just notes they cleared the band.
BOT_BASELINE_MAX = _int("BOT_BASELINE_MAX", 8)
# a server whose recent median is at or under this is "normally empty" and
# eligible for burst detection. Raise it to make the detector more eager,
# lower it to require an emptier baseline before flagging.
BOT_SPIKE_POLLS = _int("BOT_SPIKE_POLLS", 2)
# the burst must appear within this many polls; also controls how many
# freshest readings are excluded from the baseline so a building burst
# cannot raise the baseline it is being measured against.
BOT_BASELINE_WINDOW = _int("BOT_BASELINE_WINDOW", 30)
# how many recent snapshots define the baseline median (~30 minutes at
# the default 1-minute poll interval).

# Plateau pattern: organic player counts wobble by 1-2 every minute as people
# come and go. A count that holds perfectly steady across many polls is a
# bot-fill tell (the spawned clients all sit idle and never leave).
BOT_PLATEAU_POLLS = _int("BOT_PLATEAU_POLLS", 8)
BOT_PLATEAU_MIN = _int("BOT_PLATEAU_MIN", 20)

# Instant max-out: a normally-empty server going straight to BOT_SPIKE_MIN+
# players in a single poll, from a baseline at or below this threshold, has
# no plausible organic explanation -- a real fill ramps up over minutes.
BOT_INSTANT_MAX_BASELINE = _int("BOT_INSTANT_MAX_BASELINE", 1)

# Implausibly large burst: numbers above this on a normally near-empty server
# do not happen organically. Reported as an "alert" rather than the standard
# burst severity so the dashboard surfaces them prominently.
BOT_IMPLAUSIBLE_MIN = _int("BOT_IMPLAUSIBLE_MIN", 200)

# Ramp guard: organic growth climbs gradually -- by the time a server reaches
# burst level, the previous poll is already well on the way there. A bot fill
# is a single step from near-empty straight to fully loaded. Skip the burst
# alert when the previous poll was already at this fraction of the current
# count or higher, so organic growth (e.g. a popular event filling up over
# several minutes) is never mistaken for an automated fill.
BOT_RAMP_RATIO = float(_str("BOT_RAMP_RATIO", "0.5"))

# Popular-server lenience: a server whose all-time peak is at or above this
# is treated as "known-busy". Its burst threshold is scaled up to
# (peak * BOT_POPULAR_BURST_FACTOR), so a regular show that re-fills the room
# every week is never re-flagged. Obvious tells (plateau, instant max-out,
# implausible counts) keep firing regardless -- popularity does not excuse
# perfectly-identical readings or 0->300 jumps.
BOT_POPULAR_PEAK_MIN = _int("BOT_POPULAR_PEAK_MIN", 25)
BOT_POPULAR_BURST_FACTOR = float(_str("BOT_POPULAR_BURST_FACTOR", "2.0"))

# Cross-server context. The bot also looks at every *other* server on the
# same master this poll. If the whole network is busy (median peer count
# above BOT_BUSY_NETWORK_MEDIAN), one server rising with it is probably part
# of an event, not a bot fill -- the regular burst alert is suppressed.
# Conversely, if at least BOT_COPYCAT_MIN_PEERS other servers report the
# EXACT SAME non-trivial player count (>= BOT_COPYCAT_MIN_COUNT) this poll,
# that is a copycat / coordinated-fill tell and the detection is escalated.
BOT_BUSY_NETWORK_MEDIAN = _int("BOT_BUSY_NETWORK_MEDIAN", 15)
BOT_COPYCAT_MIN_PEERS = _int("BOT_COPYCAT_MIN_PEERS", 2)
BOT_COPYCAT_MIN_COUNT = _int("BOT_COPYCAT_MIN_COUNT", 10)

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
