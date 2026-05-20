"""SQLite storage for server snapshots, polls and detected anomalies."""
import os
import re
import sqlite3
from contextlib import contextmanager

from config import DATA_DIR, DB_PATH, HB_JUMP_MARGIN

SCHEMA = """
CREATE TABLE IF NOT EXISTS servers (
    server_key TEXT PRIMARY KEY,
    ip         TEXT,
    port       INTEGER,
    name       TEXT,
    first_seen TEXT,
    last_seen  TEXT,
    status     TEXT DEFAULT 'online',
    ws_port    INTEGER,
    wss_port   INTEGER,
    source     TEXT
);
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    server_key  TEXT,
    ts          TEXT,
    name        TEXT,
    players     INTEGER,
    players_raw INTEGER,
    hbcounter   INTEGER
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
CREATE TABLE IF NOT EXISTS poll_sources (
    poll_id      INTEGER,
    source       TEXT,
    server_count INTEGER,
    player_count INTEGER
);
CREATE INDEX IF NOT EXISTS idx_pollsrc_poll ON poll_sources(poll_id);
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
    if "bot_baseline" not in scols:
        # The pre-spike player count, captured the moment a bot spike begins.
        # While bot_state='spike' this value is substituted into snapshots and
        # poll totals so the inflated readings never contaminate the data.
        c.execute("ALTER TABLE servers ADD COLUMN bot_baseline INTEGER")
    if "ws_port" not in scols:
        c.execute("ALTER TABLE servers ADD COLUMN ws_port INTEGER")
    if "wss_port" not in scols:
        c.execute("ALTER TABLE servers ADD COLUMN wss_port INTEGER")
    if "source" not in scols:
        c.execute("ALTER TABLE servers ADD COLUMN source TEXT")
    if "description" not in scols:
        # Free-text blurb the server itself publishes on the master list --
        # surfaced in the dashboard so each server's room concept is visible
        # without needing to click in and join. Servers that do not publish
        # one keep it NULL; it is refreshed every poll a server is seen on.
        c.execute("ALTER TABLE servers ADD COLUMN description TEXT")

    sncols = {r["name"] for r in c.execute("PRAGMA table_info(snapshots)")}
    if "players_raw" not in sncols:
        # Forensic copy of the raw master-server reading. `players` may hold a
        # substituted baseline while a spike is in progress; `players_raw`
        # always holds what the master actually reported, so the unfiltered
        # series is recoverable for review without re-contaminating graphs.
        c.execute("ALTER TABLE snapshots ADD COLUMN players_raw INTEGER")
        c.execute(
            "UPDATE snapshots SET players_raw = players "
            "WHERE players_raw IS NULL")

    # Per-master poll breakdown is newer than the polls table. If it is empty
    # but polls exist, backfill it once: attribute each past poll's snapshots
    # to the master that currently lists each server (the only source we have
    # for historical data). New polls are recorded per-master directly.
    have_src = c.execute("SELECT COUNT(*) n FROM poll_sources").fetchone()["n"]
    have_polls = c.execute(
        "SELECT COUNT(*) n FROM polls WHERE ok=1").fetchone()["n"]
    if have_src == 0 and have_polls:
        c.execute(
            "INSERT INTO poll_sources (poll_id, source, server_count, "
            "                          player_count) "
            "SELECT p.id, sv.source, COUNT(*), "
            "       COALESCE(SUM(sn.players), 0) "
            "FROM polls p "
            "JOIN snapshots sn ON sn.ts = p.ts "
            "JOIN servers sv ON sv.server_key = sn.server_key "
            "WHERE p.ok=1 "
            "GROUP BY p.id, sv.source")

    # Earlier builds flagged routine upward HB jumps: the master server only
    # publishes the counter every few minutes, so a +20-30 step is just
    # accumulated time, not tampering. Purge those stale sub-threshold
    # hb_jump anomalies so they no longer show as suspicious anywhere.
    stale = []
    for r in c.execute("SELECT id, detail FROM anomalies WHERE type='hb_jump'"):
        m = re.search(r"jumped \+(\d+)", r["detail"] or "")
        if m and int(m.group(1)) <= HB_JUMP_MARGIN:
            stale.append((r["id"],))
    if stale:
        c.executemany("DELETE FROM anomalies WHERE id=?", stale)

    # Earlier builds plateau-flagged any non-trivial player count held flat
    # across BOT_PLATEAU_POLLS bot polls, without accounting for the vanilla
    # AO master's ~5-minute publish cadence (so ~4 in every 5 polls were
    # cached re-reads) and without requiring a near-empty baseline (so an
    # organically busy server triggered it just by staying steady during an
    # RP session). The new detector dedupes by hbcounter and requires the
    # baseline guard, so those old alerts were noise. Purge them, restore
    # the snapshots whose `players` got substituted to the captured baseline
    # while the spike was open, re-aggregate the affected poll totals, and
    # clear any server still stuck in `bot_state='spike'` because its count
    # never crossed BOT_SPIKE_MIN (so the spike-end branch never fired).
    plateau_anoms = c.execute(
        "SELECT id, ts, server_key FROM anomalies "
        "WHERE type='bot_spike' AND detail LIKE 'Player count held flat at%' "
        "ORDER BY ts").fetchall()
    affected_ts = set()
    drop_ids = []
    stuck_keys = set()
    for sp in plateau_anoms:
        drop_ids.append(sp["id"])
        end = c.execute(
            "SELECT id, ts FROM anomalies "
            "WHERE type='bot_spike_end' AND server_key=? AND ts > ? "
            "ORDER BY ts LIMIT 1",
            (sp["server_key"], sp["ts"])).fetchone()
        if end is not None:
            drop_ids.append(end["id"])
            window = c.execute(
                "SELECT ts FROM snapshots "
                "WHERE server_key=? AND ts>=? AND ts<? "
                "AND players!=players_raw",
                (sp["server_key"], sp["ts"], end["ts"])).fetchall()
            for w in window:
                affected_ts.add(w["ts"])
            c.execute(
                "UPDATE snapshots SET players=players_raw "
                "WHERE server_key=? AND ts>=? AND ts<? "
                "AND players!=players_raw",
                (sp["server_key"], sp["ts"], end["ts"]))
        else:
            stuck_keys.add(sp["server_key"])
            window = c.execute(
                "SELECT ts FROM snapshots "
                "WHERE server_key=? AND ts>=? AND players!=players_raw",
                (sp["server_key"], sp["ts"])).fetchall()
            for w in window:
                affected_ts.add(w["ts"])
            c.execute(
                "UPDATE snapshots SET players=players_raw "
                "WHERE server_key=? AND ts>=? AND players!=players_raw",
                (sp["server_key"], sp["ts"]))
    if drop_ids:
        c.executemany("DELETE FROM anomalies WHERE id=?",
                      [(i,) for i in drop_ids])
    if stuck_keys:
        c.executemany(
            "UPDATE servers SET bot_state='normal', bot_baseline=NULL "
            "WHERE server_key=?", [(k,) for k in stuck_keys])
    if affected_ts:
        c.executemany(
            "UPDATE polls SET player_count = ("
            "  SELECT COALESCE(SUM(players), 0) FROM snapshots "
            "  WHERE snapshots.ts = polls.ts"
            ") WHERE ts=?", [(t,) for t in affected_ts])
        c.executemany(
            "UPDATE poll_sources SET player_count = ("
            "  SELECT COALESCE(SUM(sn.players), 0) "
            "  FROM snapshots sn JOIN servers sv "
            "       ON sv.server_key = sn.server_key "
            "  WHERE sn.ts = (SELECT ts FROM polls WHERE id=poll_sources.poll_id) "
            "  AND sv.source = poll_sources.source"
            ") WHERE poll_id IN ("
            "  SELECT id FROM polls WHERE ts=?)",
            [(t,) for t in affected_ts])


# --- polls ---

def record_poll(ts, ok, server_count, player_count=0, by_source=None):
    """Store one poll. `by_source` maps a master URL to its own
    {"servers": n, "players": n} so the per-master trend stays separate."""
    with _db() as c:
        cur = c.execute(
            "INSERT INTO polls (ts, ok, server_count, player_count) "
            "VALUES (?,?,?,?)",
            (ts, 1 if ok else 0, server_count, player_count))
        if by_source:
            c.executemany(
                "INSERT INTO poll_sources (poll_id, source, server_count, "
                "                          player_count) VALUES (?,?,?,?)",
                [(cur.lastrowid, src, agg["servers"], agg["players"])
                 for src, agg in by_source.items()])


def poll_history(since=None, until=None):
    """Successful polls oldest-first, for the global player-count graph.

    `since` and `until` are optional ISO timestamp bounds. `since` is
    inclusive (>=); `until` is exclusive (<), so passing the start of the
    next day yields exactly one day's polls when paired with that day's
    start as `since`.
    """
    q = "SELECT ts, server_count, player_count FROM polls WHERE ok=1"
    params = []
    if since is not None:
        q += " AND ts>=?"
        params.append(since)
    if until is not None:
        q += " AND ts<?"
        params.append(until)
    q += " ORDER BY id"
    with _db() as c:
        return c.execute(q, params).fetchall()


def poll_source_history(since=None, until=None):
    """Per-master player/server counts for each successful poll, oldest-first.

    Returns rows of (ts, source, server_count, player_count) so the dashboard
    can draw one trend line per master server.
    """
    q = ("SELECT p.ts AS ts, ps.source AS source, "
         "       ps.server_count AS server_count, "
         "       ps.player_count AS player_count "
         "FROM poll_sources ps JOIN polls p ON p.id = ps.poll_id "
         "WHERE p.ok=1")
    params = []
    if since is not None:
        q += " AND p.ts>=?"
        params.append(since)
    if until is not None:
        q += " AND p.ts<?"
        params.append(until)
    q += " ORDER BY p.id"
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


def upsert_server(key, ip, port, name, now, ws_port=None, wss_port=None,
                  source=None, description=None):
    with _db() as c:
        c.execute(
            """INSERT INTO servers
                 (server_key, ip, port, name, first_seen, last_seen, status,
                  ws_port, wss_port, source, description)
               VALUES (?,?,?,?,?,?,'online',?,?,?,?)
               ON CONFLICT(server_key) DO UPDATE SET
                 name=excluded.name, last_seen=excluded.last_seen,
                 status='online', ws_port=excluded.ws_port,
                 wss_port=excluded.wss_port, source=excluded.source,
                 description=excluded.description""",
            (key, ip, port, name, now, now, ws_port, wss_port, source,
             description))


def touch_server(key, name, now, ws_port=None, wss_port=None, source=None,
                 description=None):
    with _db() as c:
        c.execute(
            "UPDATE servers SET name=?, last_seen=?, status='online', "
            "ws_port=?, wss_port=?, source=?, description=? "
            "WHERE server_key=?",
            (name, now, ws_port, wss_port, source, description, key))


def set_server_status(key, status):
    with _db() as c:
        c.execute("UPDATE servers SET status=? WHERE server_key=?", (status, key))


def online_servers():
    with _db() as c:
        return c.execute("SELECT * FROM servers WHERE status='online'").fetchall()


def all_servers():
    with _db() as c:
        return c.execute("SELECT * FROM servers ORDER BY name").fetchall()


def dead_servers(cutoff_iso):
    """Servers whose last appearance on the master list predates `cutoff_iso`.

    These have not been seen for the configured dead-server window and are
    treated as shut down. Their stored history is left untouched; a server
    that re-pings the list has its last_seen refreshed and stops matching.
    Returned oldest-disappearance first.
    """
    with _db() as c:
        return c.execute(
            "SELECT * FROM servers WHERE last_seen < ? ORDER BY last_seen",
            (cutoff_iso,)).fetchall()


def set_bot_state(key, state, baseline=None):
    """Update a server's bot-pattern state and its captured pre-spike baseline.

    `baseline` is the player count substituted into snapshots while in
    `spike` state; pass None to clear it (typically on the spike-end edge).
    """
    with _db() as c:
        c.execute(
            "UPDATE servers SET bot_state=?, bot_baseline=? "
            "WHERE server_key=?", (state, baseline, key))


def find_servers(query):
    like = f"%{query}%"
    with _db() as c:
        return c.execute(
            "SELECT * FROM servers WHERE name LIKE ? OR server_key LIKE ? ORDER BY name",
            (like, like)).fetchall()


def find_servers_by_ip(ip):
    """Every tracked server hosted at `ip`, across all ports."""
    with _db() as c:
        return c.execute(
            "SELECT * FROM servers WHERE ip=? ORDER BY port", (ip,)).fetchall()


def delete_server_data(key):
    """Purge every trace of one server: its row, all snapshots, all anomalies.

    Returns a {"snapshots": n, "anomalies": n, "server": 0|1} tally so the
    caller can report what was removed.
    """
    with _db() as c:
        snaps = c.execute(
            "DELETE FROM snapshots WHERE server_key=?", (key,)).rowcount
        anoms = c.execute(
            "DELETE FROM anomalies WHERE server_key=?", (key,)).rowcount
        srv = c.execute(
            "DELETE FROM servers WHERE server_key=?", (key,)).rowcount
        return {"snapshots": snaps, "anomalies": anoms, "server": srv}


# --- snapshots ---

def add_snapshot(key, ts, name, players, hb, players_raw=None):
    """Store one server snapshot.

    `players` is the value safe to graph -- a substituted baseline during a
    bot spike, the master-reported count otherwise. `players_raw` is the
    unfiltered master-reported count, kept for forensic review and defaults
    to `players` when no separate raw value is supplied.
    """
    if players_raw is None:
        players_raw = players
    with _db() as c:
        c.execute(
            "INSERT INTO snapshots "
            "(server_key, ts, name, players, players_raw, hbcounter) "
            "VALUES (?,?,?,?,?,?)",
            (key, ts, name, players, players_raw, hb))


def latest_snapshot(key):
    with _db() as c:
        return c.execute(
            "SELECT * FROM snapshots WHERE server_key=? ORDER BY id DESC LIMIT 1",
            (key,)).fetchone()


def server_history(key, limit=None, since=None, until=None):
    """Snapshots for a server, oldest-first.

    limit=None returns full history. `since` and `until` are optional ISO
    timestamp bounds (since inclusive, until exclusive) used to scope the
    history to a day / week / month / year / specific calendar date.
    """
    where = "server_key=?"
    params = [key]
    if since is not None:
        where += " AND ts>=?"
        params.append(since)
    if until is not None:
        where += " AND ts<?"
        params.append(until)
    with _db() as c:
        if limit is None:
            return c.execute(
                f"SELECT * FROM snapshots WHERE {where} ORDER BY id",
                params).fetchall()
        rows = c.execute(
            f"SELECT * FROM snapshots WHERE {where} ORDER BY id DESC LIMIT ?",
            params + [limit]).fetchall()
    return list(reversed(rows))


def server_stats(since=None, until=None):
    """Per-server aggregates for the all-server comparison.

    Returns one row per server with snaps (snapshot count), peak and mean
    player counts -- computed in SQL so the full snapshot table never has to
    be loaded into memory. `since` and `until` are optional ISO bounds.
    """
    q = ("SELECT server_key, COUNT(*) snaps, "
         "MAX(players) peak, AVG(players) mean FROM snapshots")
    clauses = []
    params = []
    if since is not None:
        clauses.append("ts>=?")
        params.append(since)
    if until is not None:
        clauses.append("ts<?")
        params.append(until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " GROUP BY server_key"
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


def anomaly_counts(since=None, until=None):
    """Per-server anomaly tallies, keyed by server_key.

    Returns {server_key: {"total": n, "alerts": n, "bot_spikes": n}}.
    """
    q = ("SELECT server_key, "
         "COUNT(*) total, "
         "SUM(CASE WHEN severity='alert' THEN 1 ELSE 0 END) alerts, "
         "SUM(CASE WHEN type='bot_spike' THEN 1 ELSE 0 END) bot_spikes "
         "FROM anomalies")
    clauses, params = [], []
    if since is not None:
        clauses.append("ts>=?"); params.append(since)
    if until is not None:
        clauses.append("ts<?"); params.append(until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " GROUP BY server_key"
    with _db() as c:
        return {r["server_key"]: {"total": r["total"],
                                  "alerts": r["alerts"] or 0,
                                  "bot_spikes": r["bot_spikes"] or 0}
                for r in c.execute(q, params).fetchall()}


def query_anomalies(server_key=None, type_=None, severity=None,
                    since=None, until=None, limit=500):
    """Flexible anomaly search for the web dashboard, newest-first."""
    q = "SELECT * FROM anomalies WHERE 1=1"
    params = []
    if server_key:
        q += " AND server_key=?"
        params.append(server_key)
    if type_:
        q += " AND type=?"
        params.append(type_)
    if severity:
        q += " AND severity=?"
        params.append(severity)
    if since is not None:
        q += " AND ts>=?"
        params.append(since)
    if until is not None:
        q += " AND ts<?"
        params.append(until)
    q += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with _db() as c:
        return c.execute(q, params).fetchall()


def anomaly_type_counts(since=None, until=None):
    """How many anomalies of each type exist, as {type: count}."""
    q = "SELECT type, COUNT(*) n FROM anomalies"
    clauses, params = [], []
    if since is not None:
        clauses.append("ts>=?"); params.append(since)
    if until is not None:
        clauses.append("ts<?"); params.append(until)
    if clauses:
        q += " WHERE " + " AND ".join(clauses)
    q += " GROUP BY type ORDER BY n DESC"
    with _db() as c:
        return {r["type"]: r["n"] for r in c.execute(q, params).fetchall()}


def bot_attempts_by_master(since=None, until=None, recent_limit=5):
    """Suspected bot fills tallied per master server.

    Joins anomalies of type `bot_spike` to their server's `source` so the
    dashboard can show how many bot-fill attempts each master has seen, how
    many distinct servers were affected, the most recent attempt's timestamp,
    and a short list of the latest offenders. Returns a list of dicts,
    busiest master first:
        {"source": url, "attempts": n, "servers": n, "last_ts": iso,
         "currently_spiking": n, "recent": [{server_key, name, ts}, ...]}
    Servers that were renamed or have disappeared still appear in the count
    because the join keeps the original anomaly rows.
    """
    where = "a.type='bot_spike'"
    params = []
    if since is not None:
        where += " AND a.ts>=?"
        params.append(since)
    if until is not None:
        where += " AND a.ts<?"
        params.append(until)
    with _db() as c:
        rows = c.execute(
            "SELECT COALESCE(sv.source, '(unknown)') AS source, "
            "       COUNT(*) AS attempts, "
            "       COUNT(DISTINCT a.server_key) AS servers, "
            "       MAX(a.ts) AS last_ts "
            "FROM anomalies a "
            "LEFT JOIN servers sv ON sv.server_key = a.server_key "
            f"WHERE {where} "
            "GROUP BY source ORDER BY attempts DESC", params).fetchall()
        spiking = {r["source"] or "(unknown)": r["n"] for r in c.execute(
            "SELECT COALESCE(source,'(unknown)') AS source, COUNT(*) n "
            "FROM servers WHERE bot_state='spike' "
            "GROUP BY source").fetchall()}
        out = []
        for r in rows:
            recents = c.execute(
                "SELECT a.server_key, a.name, a.ts "
                "FROM anomalies a "
                "LEFT JOIN servers sv ON sv.server_key = a.server_key "
                f"WHERE {where} AND COALESCE(sv.source,'(unknown)')=? "
                "ORDER BY a.id DESC LIMIT ?",
                params + [r["source"], recent_limit]).fetchall()
            out.append({
                "source": r["source"],
                "attempts": r["attempts"],
                "servers": r["servers"],
                "last_ts": r["last_ts"],
                "currently_spiking": spiking.get(r["source"], 0),
                "recent": [dict(x) for x in recents],
            })
        return out


def integrity_counts(since=None, until=None):
    """Per-server count of HB-integrity anomalies, keyed by server_key."""
    q = ("SELECT server_key, COUNT(*) n FROM anomalies "
         "WHERE type IN ('hb_drop','hb_jump','hb_reset')")
    params = []
    if since is not None:
        q += " AND ts>=?"
        params.append(since)
    if until is not None:
        q += " AND ts<?"
        params.append(until)
    q += " GROUP BY server_key"
    with _db() as c:
        return {r["server_key"]: r["n"]
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
