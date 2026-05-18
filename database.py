"""SQLite storage for server snapshots, polls and detected anomalies."""
import os
import sqlite3
from contextlib import contextmanager

from config import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    server_key TEXT PRIMARY KEY,
    ip         TEXT,
    port       INTEGER,
    name       TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    status     TEXT DEFAULT 'online'
);
CREATE TABLE IF NOT EXISTS snapshots (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    server_key TEXT,
    ts         TEXT,
    name       TEXT,
    players    INTEGER,
    hbcounter  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_snap_key_ts ON snapshots(server_key, ts);
CREATE INDEX IF NOT EXISTS idx_snap_ts ON snapshots(ts);
CREATE TABLE IF NOT EXISTS polls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT,
    ok           INTEGER,
    server_count INTEGER,
    player_count INTEGER
);
CREATE TABLE IF NOT EXISTS anomalies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT,
    server_key TEXT,
    name       TEXT,
    type       TEXT,
    severity   TEXT,
    detail     TEXT
);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with _db() as c:
        c.executescript(SCHEMA)
        _migrate(c)


def _migrate(c):
    """Bring an older database up to the current schema."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(polls)")}
    if "player_count" not in cols:
        c.execute("ALTER TABLE polls ADD COLUMN player_count INTEGER")
        # Backfill the global player count of past polls from stored snapshots.
        c.execute(
            "UPDATE polls SET player_count = ("
            "  SELECT COALESCE(SUM(players), 0) FROM snapshots"
            "  WHERE snapshots.ts = polls.ts"
            ") WHERE player_count IS NULL AND ok=1")

    scols = {r["name"] for r in c.execute("PRAGMA table_info(servers)")}
    if "bot_state" not in scols:
        c.execute(
            "ALTER TABLE servers ADD COLUMN bot_state TEXT DEFAULT 'normal'")


# --- polls ---

def record_poll(ts, ok, server_count, player_count=0):
    with _db() as c:
        c.execute(
            "INSERT INTO polls (ts, ok, server_count, player_count) "
            "VALUES (?,?,?,?)",
            (ts, 1 if ok else 0, server_count, player_count))


def poll_history(since=None):
    """Successful polls oldest-first, for the global player-count graph.

    `since` is an optional ISO timestamp lower bound (inclusive).
    """
    q = "SELECT ts, server_count, player_count FROM polls WHERE ok=1"
    params = []
    if since is not None:
        q += " AND ts>=?"
        params.append(since)
    q += " ORDER BY id"
    with _db() as c:
        return c.execute(q, params).fetchall()


def get_last_successful_poll():
    with _db() as c:
        return c.execute(
            "SELECT * FROM polls WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()


# --- servers ---

def get_server(key):
    with _db() as c:
        return c.execute(
            "SELECT * FROM servers WHERE server_key=?", (key,)).fetchone()


def upsert_server(key, ip, port, name, now):
    with _db() as c:
        c.execute(
            """INSERT INTO servers (server_key, ip, port, name, first_seen, last_seen, status)
               VALUES (?,?,?,?,?,?,'online')
               ON CONFLICT(server_key) DO UPDATE SET
                 name=excluded.name, last_seen=excluded.last_seen, status='online'""",
            (key, ip, port, name, now, now))


def touch_server(key, name, now):
    with _db() as c:
        c.execute(
            "UPDATE servers SET name=?, last_seen=?, status='online' WHERE server_key=?",
            (name, now, key))


def set_server_status(key, status):
    with _db() as c:
        c.execute("UPDATE servers SET status=? WHERE server_key=?", (status, key))


def online_servers():
    with _db() as c:
        return c.execute("SELECT * FROM servers WHERE status='online'").fetchall()


def all_servers():
    with _db() as c:
        return c.execute("SELECT * FROM servers ORDER BY name").fetchall()


def set_bot_state(key, state):
    with _db() as c:
        c.execute("UPDATE servers SET bot_state=? WHERE server_key=?",
                  (state, key))


def find_servers(query):
    like = f"%{query}%"
    with _db() as c:
        return c.execute(
            "SELECT * FROM servers WHERE name LIKE ? OR server_key LIKE ? ORDER BY name",
            (like, like)).fetchall()


# --- snapshots ---

def add_snapshot(key, ts, name, players, hb):
    with _db() as c:
        c.execute(
            "INSERT INTO snapshots (server_key, ts, name, players, hbcounter) "
            "VALUES (?,?,?,?,?)", (key, ts, name, players, hb))


def latest_snapshot(key):
    with _db() as c:
        return c.execute(
            "SELECT * FROM snapshots WHERE server_key=? ORDER BY id DESC LIMIT 1",
            (key,)).fetchone()


def server_history(key, limit=None, since=None):
    """Snapshots for a server, oldest-first.

    limit=None returns full history. `since` is an optional ISO timestamp
    lower bound (inclusive) used to graph by day/week/month/year.
    """
    where = "server_key=?"
    params = [key]
    if since is not None:
        where += " AND ts>=?"
        params.append(since)
    with _db() as c:
        if limit is None:
            return c.execute(
                f"SELECT * FROM snapshots WHERE {where} ORDER BY id",
                params).fetchall()
        rows = c.execute(
            f"SELECT * FROM snapshots WHERE {where} ORDER BY id DESC LIMIT ?",
            params + [limit]).fetchall()
    return list(reversed(rows))


def all_snapshots(since=None):
    """Every server's snapshots oldest-first, for the all-server comparison.

    `since` is an optional ISO timestamp lower bound (inclusive).
    """
    q = "SELECT server_key, ts, name, players, hbcounter FROM snapshots"
    params = []
    if since is not None:
        q += " WHERE ts>=?"
        params.append(since)
    q += " ORDER BY id"
    with _db() as c:
        return c.execute(q, params).fetchall()


def last_poll_servers():
    with _db() as c:
        last = c.execute(
            "SELECT ts FROM polls WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        if not last:
            return []
        return c.execute(
            "SELECT * FROM snapshots WHERE ts=?", (last["ts"],)).fetchall()


# --- anomalies ---

def add_anomaly(a):
    with _db() as c:
        c.execute(
            "INSERT INTO anomalies (ts, server_key, name, type, severity, detail) "
            "VALUES (?,?,?,?,?,?)",
            (a["ts"], a["server_key"], a["name"], a["type"], a["severity"], a["detail"]))


def recent_anomalies(limit=15, alerts_only=False):
    q = "SELECT * FROM anomalies"
    if alerts_only:
        q += " WHERE severity='alert'"
    q += " ORDER BY id DESC LIMIT ?"
    with _db() as c:
        return c.execute(q, (limit,)).fetchall()


def server_anomalies(key, limit=10):
    with _db() as c:
        return c.execute(
            "SELECT * FROM anomalies WHERE server_key=? ORDER BY id DESC LIMIT ?",
            (key, limit)).fetchall()


def anomaly_counts(since=None):
    """Per-server anomaly tallies, keyed by server_key.

    Returns {server_key: {"total": n, "alerts": n, "bot_spikes": n}}.
    """
    q = ("SELECT server_key, "
         "COUNT(*) total, "
         "SUM(CASE WHEN severity='alert' THEN 1 ELSE 0 END) alerts, "
         "SUM(CASE WHEN type='bot_spike' THEN 1 ELSE 0 END) bot_spikes "
         "FROM anomalies")
    params = []
    if since is not None:
        q += " WHERE ts>=?"
        params.append(since)
    q += " GROUP BY server_key"
    with _db() as c:
        return {r["server_key"]: {"total": r["total"],
                                  "alerts": r["alerts"] or 0,
                                  "bot_spikes": r["bot_spikes"] or 0}
                for r in c.execute(q, params).fetchall()}


# --- stats ---

def stats():
    with _db() as c:
        def n(sql):
            return c.execute(sql).fetchone()["n"]
        last = c.execute(
            "SELECT ts FROM polls WHERE ok=1 ORDER BY id DESC LIMIT 1").fetchone()
        return {
            "polls": n("SELECT COUNT(*) n FROM polls WHERE ok=1"),
            "failed": n("SELECT COUNT(*) n FROM polls WHERE ok=0"),
            "snapshots": n("SELECT COUNT(*) n FROM snapshots"),
            "known": n("SELECT COUNT(*) n FROM servers"),
            "online": n("SELECT COUNT(*) n FROM servers WHERE status='online'"),
            "anomalies": n("SELECT COUNT(*) n FROM anomalies"),
            "alerts": n("SELECT COUNT(*) n FROM anomalies WHERE severity='alert'"),
            "bot_spikes": n(
                "SELECT COUNT(*) n FROM anomalies WHERE type='bot_spike'"),
            "last_poll": last["ts"] if last else None,
        }
