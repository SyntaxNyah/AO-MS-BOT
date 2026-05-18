"""Optional live web dashboard.

Runs inside the bot's own event loop and serves a multi-page dashboard plus
a JSON API, all reading from the same SQLite database the bot writes. It is
strictly read-only -- it never polls or posts anything.

Enable it with WEBSITE_ENABLED=1; everything else has a sensible default.
"""
import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web

import config
import database as db

log = logging.getLogger("website")

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

INTEGRITY_TYPES = ("hb_drop", "hb_jump", "hb_reset")

_PERIODS = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}

# All-server reliability tiers for the comparison page; matched first-to-last.
_TIERS = [
    ("Rock solid", "rock", 90.0, 100.01),
    ("Stable", "stable", 50.0, 90.0),
    ("Flaky", "flaky", 20.0, 50.0),
    ("Rarely online", "rare", 0.0, 20.0),
]


def _since(period):
    """Turn a period name into an ISO lower bound, or None for all history."""
    delta = _PERIODS.get(period)
    if delta is None:
        return None
    return (datetime.now(timezone.utc) - delta).isoformat()


def _downsample(rows, limit=600):
    """Thin a long row list to at most `limit` points, keeping the last one."""
    n = len(rows)
    if n <= limit:
        return list(rows)
    step = n / limit
    out = [rows[int(i * step)] for i in range(limit)]
    out[-1] = rows[-1]
    return out


def _daily(rows, ts_key, val_key):
    """Bucket rows into per-day peak / low / average of one numeric field."""
    buckets = {}
    for r in rows:
        v = r[val_key]
        if v is None:
            continue
        day = r[ts_key][:10]
        b = buckets.get(day)
        if b is None:
            buckets[day] = [v, v, v, 1]          # peak, low, sum, count
        else:
            b[0] = max(b[0], v)
            b[1] = min(b[1], v)
            b[2] += v
            b[3] += 1
    return [{"day": d, "peak": b[0], "low": b[1],
             "avg": round(b[2] / b[3], 1)}
            for d, b in sorted(buckets.items())]


def _uptime(snaps, poll_count):
    if not poll_count:
        return 0.0
    return round(min(100.0 * snaps / poll_count, 100.0), 1)


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------

async def api_overview(request):
    """Headline page: stats, server list, anomaly feed, player trend."""
    period = request.query.get("period", "all")
    since = _since(period)

    stats = await asyncio.to_thread(db.stats)
    snaps = await asyncio.to_thread(db.last_poll_servers)
    servers = await asyncio.to_thread(db.all_servers)
    anomalies = await asyncio.to_thread(db.recent_anomalies, 60, False)
    polls = await asyncio.to_thread(db.poll_history, since)

    status = {s["server_key"]: s["status"] for s in servers}
    server_list = sorted(
        ({"key": s["server_key"], "name": s["name"],
          "players": s["players"], "hb": s["hbcounter"],
          "status": status.get(s["server_key"], "online")}
         for s in snaps),
        key=lambda s: s["players"], reverse=True)

    players = [{"t": r["ts"], "p": r["player_count"] or 0,
                "s": r["server_count"] or 0}
               for r in _downsample(polls)
               if r["player_count"] is not None]

    return web.json_response({
        "stats": dict(stats),
        "servers": server_list,
        "anomalies": [dict(a) for a in anomalies],
        "players": players,
        "period": period,
        "now": datetime.now(timezone.utc).isoformat(),
    })


async def api_servers(request):
    """Every server ever tracked, with full aggregate stats."""
    period = request.query.get("period", "all")
    since = _since(period)

    servers = await asyncio.to_thread(db.all_servers)
    stat_rows = await asyncio.to_thread(db.server_stats, since)
    counts = await asyncio.to_thread(db.anomaly_counts, since)
    polls = await asyncio.to_thread(db.poll_history, since)
    last_snaps = await asyncio.to_thread(db.last_poll_servers)

    poll_count = len(polls)
    stat_by = {r["server_key"]: r for r in stat_rows}
    snap_by = {s["server_key"]: s for s in last_snaps}

    out = []
    for s in servers:
        k = s["server_key"]
        st = stat_by.get(k)
        c = counts.get(k, {})
        ls = snap_by.get(k)
        snaps = st["snaps"] if st else 0
        out.append({
            "key": k, "name": s["name"], "status": s["status"],
            "first_seen": s["first_seen"], "last_seen": s["last_seen"],
            "players": ls["players"] if ls else None,
            "hb": ls["hbcounter"] if ls else None,
            "peak": (st["peak"] or 0) if st else 0,
            "mean": round((st["mean"] or 0.0), 1) if st else 0.0,
            "snaps": snaps,
            "uptime": _uptime(snaps, poll_count),
            "anomalies": c.get("total", 0),
            "alerts": c.get("alerts", 0),
            "bot_spikes": c.get("bot_spikes", 0),
        })
    return web.json_response({"servers": out, "polls": poll_count,
                              "period": period})


async def api_server(request):
    """Detail, history, daily breakdown and anomalies for one server."""
    key = request.query.get("key", "")
    period = request.query.get("period", "all")
    since = _since(period)

    srv = await asyncio.to_thread(db.get_server, key)
    if srv is None:
        return web.json_response({"error": "Unknown server."}, status=404)

    snap = await asyncio.to_thread(db.latest_snapshot, key)
    hist = await asyncio.to_thread(db.server_history, key, None, since)
    anoms = await asyncio.to_thread(db.server_anomalies, key, 100)

    sampled = _downsample(hist)
    return web.json_response({
        "server": dict(srv),
        "snapshot": dict(snap) if snap else None,
        "history": [{"t": r["ts"], "p": r["players"], "hb": r["hbcounter"]}
                    for r in sampled],
        "daily": _daily(hist, "ts", "players"),
        "anomalies": [dict(a) for a in anoms],
        "points": len(hist),
        "period": period,
    })


async def api_players(request):
    """Global player count: continuous trend and per-day peak/low."""
    period = request.query.get("period", "all")
    since = _since(period)
    polls = await asyncio.to_thread(db.poll_history, since)
    valid = [r for r in polls if r["player_count"] is not None]

    trend = [{"t": r["ts"], "p": r["player_count"] or 0,
              "s": r["server_count"] or 0}
             for r in _downsample(valid, 900)]
    return web.json_response({
        "trend": trend,
        "daily": _daily(valid, "ts", "player_count"),
        "daily_servers": _daily(valid, "ts", "server_count"),
        "polls": len(polls),
        "period": period,
    })


async def api_compare(request):
    """All-server comparison: reliability tiers and a player overlay."""
    period = request.query.get("period", "all")
    since = _since(period)

    polls = await asyncio.to_thread(db.poll_history, since)
    poll_count = len(polls)
    stat_rows = await asyncio.to_thread(db.server_stats, since)
    counts = await asyncio.to_thread(db.anomaly_counts, since)
    names = {s["server_key"]: s["name"]
             for s in await asyncio.to_thread(db.all_servers)}

    servers = []
    for r in stat_rows:
        k = r["server_key"]
        c = counts.get(k, {})
        servers.append({
            "key": k, "name": names.get(k, k),
            "peak": r["peak"] or 0, "mean": round(r["mean"] or 0.0, 1),
            "snaps": r["snaps"],
            "uptime": _uptime(r["snaps"], poll_count),
            "anomalies": c.get("total", 0), "alerts": c.get("alerts", 0),
            "bot_spikes": c.get("bot_spikes", 0),
        })

    for s in servers:
        for label, slug, lo, hi in _TIERS:
            if lo <= s["uptime"] < hi:
                s["tier"] = slug
                break
        else:
            s["tier"] = "rare"

    # The busiest servers get a plotted player time series for the overlay.
    plotted = sorted(servers, key=lambda s: (s["peak"], s["mean"]),
                     reverse=True)[:12]
    overlay = []
    for s in plotted:
        hist = await asyncio.to_thread(db.server_history, s["key"], None, since)
        overlay.append({
            "name": s["name"], "key": s["key"],
            "points": [[r["ts"], r["players"]] for r in _downsample(hist, 400)],
        })

    global_hist = [[r["ts"], r["player_count"]]
                   for r in _downsample(polls, 400)
                   if r["player_count"] is not None]

    return web.json_response({
        "servers": servers, "overlay": overlay, "global": global_hist,
        "poll_count": poll_count, "period": period,
        "tiers": [{"label": l, "slug": s} for l, s, _, _ in _TIERS],
    })


async def api_hb(request):
    """Heartbeat-counter history for every server, tampering flagged."""
    period = request.query.get("period", "all")
    since = _since(period)

    servers = await asyncio.to_thread(db.all_servers)
    integ = await asyncio.to_thread(db.integrity_counts, since)

    out = []
    for s in servers:
        k = s["server_key"]
        hist = await asyncio.to_thread(db.server_history, k, None, since)
        sampled = _downsample(hist, 200)
        latest = next((r["hbcounter"] for r in reversed(sampled)
                       if r["hbcounter"] is not None), None)
        out.append({
            "name": s["name"], "key": k, "status": s["status"],
            "suspicious": integ.get(k, 0),
            "latest_hb": latest,
            "points": [[r["ts"], r["hbcounter"]] for r in sampled
                       if r["hbcounter"] is not None],
        })
    out.sort(key=lambda s: (s["suspicious"], len(s["points"])), reverse=True)
    return web.json_response({"servers": out, "period": period})


async def api_anomalies(request):
    """Filterable anomaly browser."""
    period = request.query.get("period", "all")
    since = _since(period)
    type_ = request.query.get("type") or None
    severity = request.query.get("severity") or None
    server_key = request.query.get("key") or None

    rows = await asyncio.to_thread(
        db.query_anomalies, server_key, type_, severity, since, 600)
    type_counts = await asyncio.to_thread(db.anomaly_type_counts, since)

    return web.json_response({
        "anomalies": [dict(a) for a in rows],
        "type_counts": type_counts,
        "period": period,
    })


async def api_deadservers(request):
    """Servers absent from the master list long enough to be shut down."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.DEAD_SERVER_DAYS)).isoformat()
    rows = await asyncio.to_thread(db.dead_servers, cutoff)
    out = []
    for r in rows:
        days = (now - datetime.fromisoformat(r["last_seen"])).days
        out.append({"key": r["server_key"], "name": r["name"],
                    "first_seen": r["first_seen"], "last_seen": r["last_seen"],
                    "days": days})
    return web.json_response({"servers": out,
                              "threshold_days": config.DEAD_SERVER_DAYS})


async def api_meta(request):
    """Small constants the front-end likes to know about."""
    return web.json_response({
        "title": config.WEBSITE_TITLE,
        "poll_interval": config.POLL_INTERVAL_MINUTES,
        "dead_server_days": config.DEAD_SERVER_DAYS,
        "ms_url": config.MS_URL,
    })


async def index(request):
    path = os.path.join(WEB_DIR, "index.html")
    try:
        with open(path, encoding="utf-8") as f:
            html = f.read()
    except OSError:
        return web.Response(text="Dashboard file missing.", status=500)
    return web.Response(text=html, content_type="text/html")


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------

async def start():
    """Start the dashboard inside the running event loop. Returns the runner."""
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/meta", api_meta)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/servers", api_servers)
    app.router.add_get("/api/server", api_server)
    app.router.add_get("/api/players", api_players)
    app.router.add_get("/api/compare", api_compare)
    app.router.add_get("/api/hb", api_hb)
    app.router.add_get("/api/anomalies", api_anomalies)
    app.router.add_get("/api/deadservers", api_deadservers)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBSITE_HOST, config.WEBSITE_PORT)
    await site.start()
    log.info("web dashboard live at http://%s:%s",
             config.WEBSITE_HOST, config.WEBSITE_PORT)
    return runner
