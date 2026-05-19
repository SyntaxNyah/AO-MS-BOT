"""Fetches the master server list and turns each poll into stored history."""
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

import database as db
from anomaly import analyze_hb, analyze_players
from config import (BOT_BASELINE_WINDOW, MS_URLS, POLL_INTERVAL_MINUTES,
                    RELIABLE_GAP_FACTOR, ms_label, ms_rules)

log = logging.getLogger("monitor")


def server_key(s):
    return f"{s['ip']}:{s['port']}"


def clean_ip(ip):
    """The master list occasionally wraps an IP in markdown: [host](url)."""
    if isinstance(ip, str) and ip.startswith("[") and "](" in ip:
        return ip[1:ip.index("](")]
    return ip


async def _fetch_one(session, url, timeout):
    """Fetch and normalise a single master server's list."""
    async with session.get(
            url, timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
        resp.raise_for_status()
        data = await resp.json(content_type=None)

    out = []
    for s in data:
        if not isinstance(s, dict) or "ip" not in s or "port" not in s:
            continue
        try:
            port = int(s["port"])
        except (TypeError, ValueError):
            continue
        hb = s.get("hbcounter")
        out.append({
            "ip": str(s.get("ip")),
            "port": port,
            "players": int(s.get("players") or 0),
            "name": str(s.get("name") or "(unnamed)"),
            "description": str(s.get("description") or ""),
            "hbcounter": int(hb) if hb is not None else None,
            "ws_port": _opt_port(s.get("ws_port")),
            "wss_port": _opt_port(s.get("wss_port")),
            "source": url,
        })
    return out


async def fetch_servers(timeout=20):
    """Return the current server list, merged across all configured masters.

    Every master in MS_URLS is polled concurrently and the results are merged,
    deduplicated by ip:port (the first master to list a server wins). Masters
    that fail are skipped and logged; only if *every* master fails does this
    raise, so one dead master never blacks out the others.
    """
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(
            *(_fetch_one(session, url, timeout) for url in MS_URLS),
            return_exceptions=True)

    merged = {}
    errors = []
    for url, res in zip(MS_URLS, results):
        if isinstance(res, Exception):
            errors.append((url, res))
            log.warning("master server fetch failed (%s): %s", url, res)
            continue
        for s in res:
            merged.setdefault(server_key(s), s)

    if errors and len(errors) == len(MS_URLS):
        raise errors[0][1]
    return list(merged.values())


def _opt_port(value):
    """Parse an optional websocket port; treat missing/invalid/<=0 as absent."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if port > 0 else None


def _mk(ts, key, name, type_, severity, detail, master=None):
    """Build an anomaly record. `master` names the master server the affected
    server is listed on, so alerts for different masters stay distinguishable."""
    return {"ts": ts, "server_key": key, "name": name,
            "type": type_, "severity": severity, "detail": detail,
            "master": master}


async def run_poll():
    """Poll the master server, store snapshots and detect anomalies.

    Returns (summary, anomalies) where anomalies is a list of anomaly dicts.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    try:
        servers = await fetch_servers()
    except Exception as e:                       # noqa: BLE001 - log and report
        log.warning("master server fetch failed: %s", e)
        db.record_poll(now_iso, ok=0, server_count=0)
        return ({"ok": False, "error": str(e), "count": 0, "players": 0,
                 "anomalies": 0}, [])

    last_poll = db.get_last_successful_poll()
    if last_poll:
        prev_dt = datetime.fromisoformat(last_poll["ts"])
        elapsed_min = (now - prev_dt).total_seconds() / 60.0
    else:
        elapsed_min = POLL_INTERVAL_MINUTES
    gap_reliable = elapsed_min <= POLL_INTERVAL_MINUTES * RELIABLE_GAP_FACTOR

    anomalies = []
    current_keys = set()

    # Per-master peer table for this poll: lets the bot detector see what
    # every *other* server on the same master is doing right now. Used to
    # spot copycat counts (the same exact non-trivial value across several
    # servers) and to recognise busy-network nights where a rising count is
    # part of an event, not a one-off fill. Stored as (server_key, players)
    # so each server's own count is easy to filter out when building peers.
    peers_by_source = {}
    for ps in servers:
        peers_by_source.setdefault(ps["source"], []).append(
            (server_key(ps), ps["players"]))

    # All-time peak per server, fetched once so popular servers can be given
    # lenience without an extra DB hit inside the loop.
    server_peaks = {r["server_key"]: (r["peak"] or 0)
                    for r in db.server_stats()}

    for s in servers:
        key = server_key(s)
        current_keys.add(key)
        # Each master server has its own heartbeat rules and label; anomalies
        # for this server are analysed and tagged with whichever master it is
        # listed on, so the two masters' alerts never get mixed together.
        master = ms_label(s["source"])
        rules = ms_rules(s["source"])
        existing = db.get_server(key)
        prev_snap = db.latest_snapshot(key)
        was_online = existing is not None and existing["status"] == "online"

        if existing is None:
            db.upsert_server(key, s["ip"], s["port"], s["name"], now_iso,
                             s["ws_port"], s["wss_port"], s["source"],
                             description=s["description"])
            anomalies.append(_mk(now_iso, key, s["name"], "new_server", "info",
                                 f"New server appeared on the {master} "
                                 "master list.", master=master))
        else:
            if existing["status"] == "offline":
                anomalies.append(_mk(now_iso, key, s["name"], "reappeared", "low",
                                     f"Server is back on the {master} master "
                                     "list.", master=master))
            elif existing["name"] != s["name"]:
                anomalies.append(_mk(now_iso, key, s["name"], "name_change", "low",
                                     f"Renamed: '{existing['name']}' -> "
                                     f"'{s['name']}'.", master=master))
            db.touch_server(key, s["name"], now_iso,
                            s["ws_port"], s["wss_port"], s["source"],
                            description=s["description"])

        if prev_snap is not None and s["hbcounter"] is not None:
            gap = (now - datetime.fromisoformat(prev_snap["ts"])).total_seconds() / 60.0
            reliable = gap_reliable and was_online
            result = analyze_hb(prev_snap["hbcounter"], s["hbcounter"], gap,
                                reliable=reliable, rules=rules)
            if result:
                anomalies.append(_mk(now_iso, key, s["name"], *result,
                                     master=master))

        # Player-count / bot-pattern check: a near-empty server filling up
        # over a poll or two looks like an automated bot fill. When a spike
        # is detected the stored player count is held at the pre-spike
        # baseline so the inflated readings cannot contaminate snapshots or
        # the per-master / global poll totals -- the raw value is preserved
        # in `players_raw` for forensic review.
        stored_players = s["players"]
        if existing is not None:
            history = db.server_history(key, limit=BOT_BASELINE_WINDOW + 4)
            recent = [r["players"] for r in history]
            recent_hbs = [r["hbcounter"] for r in history]
            prev_state = existing["bot_state"] or "normal"
            prev_baseline = existing["bot_baseline"]
            # Peers = every other server on the same master this poll, so the
            # detector can see copycat counts and busy-network state.
            peers = [pcount for pkey, pcount
                     in peers_by_source.get(s["source"], [])
                     if pkey != key]
            new_state, new_baseline, filtered, p_result = analyze_players(
                recent, s["players"], prev_state, prev_baseline,
                server_peak=server_peaks.get(key),
                peer_counts=peers,
                recent_hbcounters=recent_hbs,
                cur_hbcounter=s["hbcounter"])
            if new_state != prev_state or new_baseline != prev_baseline:
                db.set_bot_state(key, new_state, new_baseline)
            if p_result:
                anomalies.append(_mk(now_iso, key, s["name"], *p_result,
                                     master=master))
            stored_players = filtered if filtered is not None else s["players"]
        s["stored_players"] = stored_players

        db.add_snapshot(key, now_iso, s["name"], stored_players,
                        s["hbcounter"], players_raw=s["players"])

    # Servers still marked online but absent from this poll have disappeared.
    for row in db.online_servers():
        if row["server_key"] not in current_keys:
            db.set_server_status(row["server_key"], "offline")
            master = ms_label(row["source"])
            anomalies.append(_mk(now_iso, row["server_key"], row["name"],
                                 "disappeared", "alert",
                                 f"Server vanished from the {master} master "
                                 "list.", master=master))

    # Aggregate the *filtered* counts: a server in a bot spike contributes its
    # captured pre-spike baseline so the inflated reading never lands in the
    # global or per-master totals. Falls back to the raw value for servers
    # the bot-pattern check did not run on (e.g. first-ever sighting).
    def _stored(s):
        return s.get("stored_players", s["players"])

    total_players = sum(_stored(s) for s in servers)
    # Keep each master server's counts separate so the trend never mixes them.
    by_source = {}
    for s in servers:
        agg = by_source.setdefault(s["source"], {"servers": 0, "players": 0})
        agg["servers"] += 1
        agg["players"] += _stored(s)
    db.record_poll(now_iso, ok=1, server_count=len(servers),
                   player_count=total_players, by_source=by_source)
    for a in anomalies:
        db.add_anomaly(a)

    summary = {
        "ok": True,
        "count": len(servers),
        "players": total_players,
        "anomalies": len(anomalies),
    }
    return (summary, anomalies)
