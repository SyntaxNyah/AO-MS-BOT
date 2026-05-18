"""Configuration loaded from the environment / .env file."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int(name, default):
    raw = (os.getenv(name) or "").strip()
    return int(raw) if raw else default


def _str(name, default):
    return (os.getenv(name) or "").strip() or default


# --- Discord ---
DISCORD_TOKEN = _str("DISCORD_TOKEN", "")
GUILD_ID = _int("GUILD_ID", 0)
EVENTS_CHANNEL_ID = _int("EVENTS_CHANNEL_ID", 0)
INTEGRITY_CHANNEL_ID = _int("INTEGRITY_CHANNEL_ID", 0)

# --- Master server polling ---
MS_URL = _str("MS_URL", "https://servers.aceattorneyonline.com/servers")
POLL_INTERVAL_MINUTES = _int("POLL_INTERVAL_MINUTES", 1)

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
