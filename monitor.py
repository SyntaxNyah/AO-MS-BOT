"""Fetches the master server list and turns each poll into stored history."""
import asyncio
import logging
from datetime import datetime, timezone

import aiohttp

import database as db
from anomaly import analyze_hb, analyze_players
from config import (BOT_BASELINE_WINDOW, MS_URLS, POLL_INTERVAL_MINUTES,
                    RELIABLE_GAP_FACTOR)

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


def _mk(ts, key, name, type_, severity, detail):
    return {"ts": ts, "server_key": key, "name": name,
            "type": type_, "severity": severity, "detail": detail}


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

    for s in servers:
        key = server_key(s)
        current_keys.add(key)
        existing = db.get_server(key)
        prev_snap = db.latest_snapshot(key)
        was_online = existing is not None and existing["status"] == "online"

        if existing is None:
            db.upsert_server(key, s["ip"], s["port"], s["name"], now_iso,
                             s["ws_port"], s["wss_port"])
            anomalies.append(_mk(now_iso, key, s["name"], "new_server", "info",
                                 "New server appeared on the master list."))
        else:
            if existing["status"] == "offline":
                anomalies.append(_mk(now_iso, key, s["name"], "reappeared", "low",
                                     "Server is back on the master list."))
            elif existing["name"] != s["name"]:
                anomalies.append(_mk(now_iso, key, s["name"], "name_change", "low",
                                     f"Renamed: '{existing['name']}' -> "
                                     f"'{s['name']}'."))
            db.touch_server(key, s["name"], now_iso,
                            s["ws_port"], s["wss_port"])

        if prev_snap is not None and s["hbcounter"] is not None:
            gap = (now - datetime.fromisoformat(prev_snap["ts"])).total_seconds() / 60.0
            reliable = gap_reliable and was_online
            result = analyze_hb(prev_snap["hbcounter"], s["hbcounter"], gap,
                                reliable=reliable)
            if result:
                anomalies.append(_mk(now_iso, key, s["name"], *result))

        # Player-count / bot-pattern check: a near-empty server filling up over
        # a poll or two looks like an automated bot fill.
        if existing is not None:
            recent = [r["players"] for r in db.server_history(
                key, limit=BOT_BASELINE_WINDOW + 4)]
            prev_state = existing["bot_state"] or "normal"
            new_state, p_result = analyze_players(
                recent, s["players"], prev_state)
            if new_state != prev_state:
                db.set_bot_state(key, new_state)
            if p_result:
                anomalies.append(_mk(now_iso, key, s["name"], *p_result))

        db.add_snapshot(key, now_iso, s["name"], s["players"], s["hbcounter"])

    # Servers still marked online but absent from this poll have disappeared.
    for row in db.online_servers():
        if row["server_key"] not in current_keys:
            db.set_server_status(row["server_key"], "offline")
            anomalies.append(_mk(now_iso, row["server_key"], row["name"],
                                 "disappeared", "alert",
                                 "Server vanished from the master list."))

    total_players = sum(s["players"] for s in servers)
    db.record_poll(now_iso, ok=1, server_count=len(servers),
                   player_count=total_players)
    for a in anomalies:
        db.add_anomaly(a)

    summary = {
        "ok": True,
        "count": len(servers),
        "players": total_players,
        "anomalies": len(anomalies),
    }
    return (summary, anomalies)
