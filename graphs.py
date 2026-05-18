"""Renders historical graphs for a single server.

The chart adapts to any time span -- from a few minutes up to years of
snapshots -- by collapsing dense minute-level data into time buckets that keep
the min/max range, so restarts and spikes survive the downsampling.
"""
import io
from collections import defaultdict
from datetime import datetime

import discord
import matplotlib.dates as mdates
from matplotlib import colormaps
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# Anomalies worth marking on the chart, grouped by how they're drawn:
#   drops     -- a counter fall nothing can explain, or a too-fast manual reset
#   rollovers -- the normal counter wrap at HB_CAP
#   restarts  -- the counter reset because the server was restarted
#   gone      -- the server dropped off the master list
#   returned  -- the server came back onto the master list
#   botspike  -- a sudden bot-like player burst on a near-empty server
_DROP_TYPES = {"hb_drop", "hb_reset"}
_ROLLOVER_TYPES = {"hb_rollover"}
_RESTART_TYPES = {"hb_restart"}
_GONE_TYPES = {"disappeared"}
_RETURN_TYPES = {"reappeared"}
_BOT_TYPES = {"bot_spike"}
# Heartbeat events normal operation cannot explain -- an unexplained drop, a
# counter slammed down too fast for a real restart, or an impossible jump.
# These are the clear signs the counter was tampered with, and are always
# drawn in bold red.
_SUSPICIOUS_TYPES = {"hb_drop", "hb_reset", "hb_jump"}

# Buckets to render at most; dense history is downsampled down to this.
_TARGET_POINTS = 900


def _parse(ts):
    return datetime.fromisoformat(ts)


def _human_span(delta):
    s = max(delta.total_seconds(), 0)
    if s < 3600:
        return f"{s / 60:.0f} min"
    if s < 86400:
        return f"{s / 3600:.1f} h"
    if s < 86400 * 365:
        return f"{s / 86400:.1f} days"
    return f"{s / 86400 / 365:.2f} years"


def _downsample(rows, target=_TARGET_POINTS):
    """Collapse oldest-first snapshot rows into <= target time buckets.

    Each bucket keeps min/max/last for the HB counter and min/max/mean for
    players, so long spans render cleanly without averaging away restarts.
    """
    pts = [{"ts": _parse(r["ts"]), "hb": r["hbcounter"],
            "players": r["players"]} for r in rows]

    if len(pts) <= target:
        return [{"ts": p["ts"],
                 "hb": p["hb"], "hb_min": p["hb"], "hb_max": p["hb"],
                 "players": p["players"],
                 "p_min": p["players"], "p_max": p["players"]} for p in pts]

    start = pts[0]["ts"].timestamp()
    end = pts[-1]["ts"].timestamp()
    width = (end - start) / target or 1.0

    buckets = {}
    for p in pts:
        idx = min(int((p["ts"].timestamp() - start) / width), target - 1)
        buckets.setdefault(idx, []).append(p)

    out = []
    for idx in sorted(buckets):
        group = buckets[idx]
        hbs = [g["hb"] for g in group if g["hb"] is not None]
        players = [g["players"] for g in group]
        out.append({
            "ts": group[len(group) // 2]["ts"],
            "hb": hbs[-1] if hbs else None,
            "hb_min": min(hbs) if hbs else None,
            "hb_max": max(hbs) if hbs else None,
            "players": sum(players) / len(players),
            "p_min": min(players),
            "p_max": max(players),
        })
    return out


def make_hb_graph(name, rows, anomalies=None, addr=None):
    """Build a PNG with HB counter and player history. `rows` is oldest-first.

    `anomalies` is an optional list of anomaly rows; counter drops and restarts
    inside the graphed window are marked on the HB axis. `addr` is the server's
    `ip:port` address, shown so identically named servers stay distinguishable.
    """
    pts = _downsample(rows)
    times = [p["ts"] for p in pts]

    def _f(v):
        return float("nan") if v is None else float(v)

    hb = [_f(p["hb"]) for p in pts]
    hb_lo = [_f(p["hb_min"]) for p in pts]
    hb_hi = [_f(p["hb_max"]) for p in pts]
    players = [p["players"] for p in pts]
    p_lo = [p["p_min"] for p in pts]
    p_hi = [p["p_max"] for p in pts]

    fig = Figure(figsize=(11, 8))
    ax1, ax2 = fig.subplots(2, 1, sharex=True,
                            gridspec_kw={"height_ratios": [3, 2]})
    fig.suptitle(f"{name} -- historical tracking",
                 fontsize=13, fontweight="bold")
    if addr:
        fig.text(0.5, 0.945, addr, ha="center", va="top",
                 fontsize=9, color="#666666", family="monospace")

    # --- HB counter ---
    ax1.fill_between(times, hb_lo, hb_hi, color="#4f9dff", alpha=0.22,
                     linewidth=0)
    ax1.plot(times, hb, color="#4f9dff", linewidth=1.4)
    ax1.set_ylabel("HB counter")
    ax1.grid(True, alpha=0.3)
    # Show the full counter value (49246) instead of matplotlib's +4.92e4 offset.
    ax1.ticklabel_format(axis="y", style="plain", useOffset=False)

    drops = rollovers = restarts = gone = returned = bot_spikes = 0
    if anomalies:
        for a in anomalies:
            try:
                t = _parse(a["ts"])
            except (TypeError, ValueError):
                continue
            if not (times[0] <= t <= times[-1]):
                continue
            atype = a["type"]
            if atype in _DROP_TYPES:
                ax1.axvline(t, color="#e8503a", linewidth=1.0, alpha=0.7)
                drops += 1
            elif atype in _ROLLOVER_TYPES:
                ax1.axvline(t, color="#9b59b6", linewidth=0.9, alpha=0.55,
                            linestyle=":")
                rollovers += 1
            elif atype in _RESTART_TYPES:
                ax1.axvline(t, color="#f0a23a", linewidth=0.9, alpha=0.55,
                            linestyle="--")
                restarts += 1
            elif atype in _GONE_TYPES:
                for ax in (ax1, ax2):
                    ax.axvline(t, color="#888888", linewidth=1.0, alpha=0.6,
                               linestyle="--")
                gone += 1
            elif atype in _RETURN_TYPES:
                for ax in (ax1, ax2):
                    ax.axvline(t, color="#41c97a", linewidth=1.2, alpha=0.75)
                returned += 1
            elif atype in _BOT_TYPES:
                ax2.axvline(t, color="#d6336c", linewidth=1.3, alpha=0.8)
                bot_spikes += 1

    legend = [
        Line2D([], [], color="#4f9dff", linewidth=1.6, label="HB counter"),
        Patch(facecolor="#4f9dff", alpha=0.22, label="min-max range"),
    ]
    if drops:
        legend.append(Line2D([], [], color="#e8503a", linewidth=1.0,
                              label=f"drop >35 ({drops})"))
    if rollovers:
        legend.append(Line2D([], [], color="#9b59b6", linewidth=1.0,
                              linestyle=":", label=f"rollover ({rollovers})"))
    if restarts:
        legend.append(Line2D([], [], color="#f0a23a", linewidth=1.0,
                              linestyle="--", label=f"restart ({restarts})"))
    if gone:
        legend.append(Line2D([], [], color="#888888", linewidth=1.0,
                              linestyle="--", label=f"went offline ({gone})"))
    if returned:
        legend.append(Line2D([], [], color="#41c97a", linewidth=1.2,
                              label=f"came back ({returned})"))
    ax1.legend(handles=legend, loc="upper left", fontsize=8, framealpha=0.9)

    # --- Players ---
    ax2.fill_between(times, p_lo, p_hi, color="#41c97a", alpha=0.22,
                     linewidth=0)
    ax2.plot(times, players, color="#41c97a", linewidth=1.4)
    ax2.set_ylabel("Players")
    ax2.set_xlabel("Time (UTC)")
    ax2.grid(True, alpha=0.3)
    if bot_spikes:
        ax2.legend(handles=[Line2D([], [], color="#d6336c", linewidth=1.3,
                                   label=f"suspected bot burst ({bot_spikes})")],
                   loc="upper left", fontsize=8, framealpha=0.9)

    # Adaptive date axis: scales from minutes to years on its own.
    locator = mdates.AutoDateLocator(maxticks=10)
    ax2.xaxis.set_major_locator(locator)
    ax2.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for label in ax2.get_xticklabels():
        label.set_rotation(20)
        label.set_horizontalalignment("right")

    # --- Statistician's summary box ---
    span = _human_span(times[-1] - times[0])
    all_players = [r["players"] for r in rows]
    peak = max(all_players) if all_players else 0
    mean = sum(all_players) / len(all_players) if all_players else 0
    summary = ((f"Address: {addr}\n" if addr else "") +
               f"Span: {span}\n"
               f"Snapshots: {len(rows)}  (plotted {len(pts)})\n"
               f"Rollovers: {rollovers}   Restarts: {restarts}   "
               f"Drops >35: {drops}\n"
               f"Went offline / came back: {gone} / {returned}\n"
               f"Suspected bot bursts: {bot_spikes}\n"
               f"Players  peak {peak} / mean {mean:.1f}")
    ax1.text(0.99, 0.97, summary, transform=ax1.transAxes,
             ha="right", va="top", fontsize=8, family="monospace",
             bbox={"boxstyle": "round", "facecolor": "#f4f4f4",
                   "edgecolor": "#cccccc", "alpha": 0.9})

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="history.png")


def _downsample_players(rows, target=_TARGET_POINTS):
    """Collapse oldest-first poll rows into <= target time buckets.

    Each bucket keeps min/max/mean of the global player count so long spans
    render without averaging away peaks and dips.
    """
    pts = [{"ts": _parse(r["ts"]), "players": r["player_count"]}
           for r in rows if r["player_count"] is not None]

    if len(pts) <= target:
        return [{"ts": p["ts"], "players": p["players"],
                 "p_min": p["players"], "p_max": p["players"]} for p in pts]

    start = pts[0]["ts"].timestamp()
    end = pts[-1]["ts"].timestamp()
    width = (end - start) / target or 1.0

    buckets = {}
    for p in pts:
        idx = min(int((p["ts"].timestamp() - start) / width), target - 1)
        buckets.setdefault(idx, []).append(p)

    out = []
    for idx in sorted(buckets):
        group = buckets[idx]
        players = [g["players"] for g in group]
        out.append({
            "ts": group[len(group) // 2]["ts"],
            "players": sum(players) / len(players),
            "p_min": min(players),
            "p_max": max(players),
        })
    return out


def make_player_graph(rows, label, view="trend"):
    """Build a PNG of the global Attorney Online player count over time.

    `rows` are successful poll rows oldest-first, each with a `player_count`.
    `view` is "trend" (continuous line with min-max range) or "daily"
    (per-day peak / low / mean breakdown).
    """
    vals = [(_parse(r["ts"]), r["player_count"])
            for r in rows if r["player_count"] is not None]
    peak_t, peak_v = max(vals, key=lambda v: v[1])
    low_t, low_v = min(vals, key=lambda v: v[1])
    mean_v = sum(v for _, v in vals) / len(vals)
    span = _human_span(vals[-1][0] - vals[0][0])

    fig = Figure(figsize=(14, 7))
    ax = fig.subplots()
    fig.suptitle("Attorney Online -- global player count",
                 fontsize=14, fontweight="bold")

    if view == "daily":
        by_day = defaultdict(list)
        for t, v in vals:
            by_day[t.date()].append(v)
        days = sorted(by_day)
        dts = [datetime(d.year, d.month, d.day) for d in days]
        peaks = [max(by_day[d]) for d in days]
        lows = [min(by_day[d]) for d in days]
        means = [sum(by_day[d]) / len(by_day[d]) for d in days]
        ax.fill_between(dts, lows, peaks, color="#41c97a", alpha=0.25,
                        linewidth=0, label="daily low-peak range")
        ax.plot(dts, peaks, color="#2e9e5b", linewidth=1.6, marker="o",
                markersize=3, label="daily peak")
        ax.plot(dts, lows, color="#e8503a", linewidth=1.4, marker="o",
                markersize=3, label="daily low")
        ax.plot(dts, means, color="#4f9dff", linewidth=1.2, linestyle="--",
                label="daily mean")
    else:
        pts = _downsample_players(rows)
        times = [p["ts"] for p in pts]
        players = [p["players"] for p in pts]
        p_lo = [p["p_min"] for p in pts]
        p_hi = [p["p_max"] for p in pts]
        ax.fill_between(times, p_lo, p_hi, color="#41c97a", alpha=0.22,
                        linewidth=0, label="min-max range")
        ax.plot(times, players, color="#2e9e5b", linewidth=1.6,
                label="players online")
        ax.plot([peak_t], [peak_v], marker="^", color="#2e9e5b",
                markersize=11, linestyle="none", label=f"peak {peak_v}")
        ax.plot([low_t], [low_v], marker="v", color="#e8503a",
                markersize=11, linestyle="none", label=f"lowest {low_v}")

    ax.axhline(mean_v, color="#4f9dff", linewidth=1.0, linestyle=":",
               alpha=0.8)
    ax.set_ylabel("Players online")
    ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.3)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)

    locator = mdates.AutoDateLocator(maxticks=12)
    ax.xaxis.set_major_locator(locator)
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
    for lbl in ax.get_xticklabels():
        lbl.set_rotation(20)
        lbl.set_horizontalalignment("right")

    summary = (f"Range:  {label}\n"
               f"Span:   {span}   Polls: {len(vals)}\n"
               f"Peak:   {peak_v}  ({peak_t:%Y-%m-%d %H:%M} UTC)\n"
               f"Lowest: {low_v}  ({low_t:%Y-%m-%d %H:%M} UTC)\n"
               f"Mean:   {mean_v:.1f}")
    ax.text(0.99, 0.97, summary, transform=ax.transAxes,
            ha="right", va="top", fontsize=9, family="monospace",
            bbox={"boxstyle": "round", "facecolor": "#f4f4f4",
                  "edgecolor": "#cccccc", "alpha": 0.9})

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="playercount.png")


def _downsample_xy(pts, target=_TARGET_POINTS):
    """Collapse an oldest-first list of (datetime, value) into <= target points.

    Each bucket keeps the mean value, enough to render a long trend line.
    """
    if len(pts) <= target:
        return pts
    start = pts[0][0].timestamp()
    end = pts[-1][0].timestamp()
    width = (end - start) / target or 1.0
    buckets = {}
    for t, v in pts:
        idx = min(int((t.timestamp() - start) / width), target - 1)
        buckets.setdefault(idx, []).append((t, v))
    out = []
    for idx in sorted(buckets):
        group = buckets[idx]
        out.append((group[len(group) // 2][0],
                    sum(v for _, v in group) / len(group)))
    return out


def make_compare_graph(servers, label, poll_count, global_history=None):
    """Build the all-server "Ultimate statistician" comparison PNG.

    `servers` is a list of dicts, each with: name, key, history (oldest-first
    list of (datetime, players)), uptime (0-100), peak, mean, anomalies,
    alerts, bot_spikes. `poll_count` is the number of polls in the window.
    `global_history` is an optional oldest-first list of (datetime, count) for
    the combined Attorney Online player count, drawn as a top panel.
    """
    ranked = sorted(servers, key=lambda s: (s["peak"], s["mean"]), reverse=True)
    line_n = min(12, len(ranked))
    bar_n = min(15, len(ranked))
    line_servers = sorted(ranked[:line_n],
                          key=lambda s: s["mean"], reverse=True)
    bar_servers = ranked[:bar_n]

    has_global = bool(global_history) and len(global_history) >= 2
    if has_global:
        fig = Figure(figsize=(15, 20))
        ax0, ax1, ax2, ax3 = fig.subplots(
            4, 1, gridspec_kw={"height_ratios": [2, 3, 2, 2]})
    else:
        fig = Figure(figsize=(15, 16))
        ax0 = None
        ax1, ax2, ax3 = fig.subplots(
            3, 1, gridspec_kw={"height_ratios": [3, 2, 2]})
    fig.suptitle("Attorney Online -- Ultimate server comparison",
                 fontsize=15, fontweight="bold")

    cmap = colormaps["tab20"]

    # --- Global player count (all servers combined) ---
    if ax0 is not None:
        gpts = _downsample_xy(global_history)
        gt = [t for t, _ in gpts]
        gv = [v for _, v in gpts]
        gpk_t, gpk_v = max(global_history, key=lambda x: x[1])
        glo_t, glo_v = min(global_history, key=lambda x: x[1])
        gmean = sum(v for _, v in global_history) / len(global_history)
        ax0.fill_between(gt, 0, gv, color="#9b59b6", alpha=0.16, linewidth=0)
        ax0.plot(gt, gv, color="#7d3c98", linewidth=2.0,
                 label="global players online")
        ax0.plot([gpk_t], [gpk_v], marker="^", color="#7d3c98", markersize=11,
                 linestyle="none", label=f"peak {gpk_v}")
        ax0.plot([glo_t], [glo_v], marker="v", color="#e8503a", markersize=11,
                 linestyle="none", label=f"lowest {glo_v}")
        ax0.axhline(gmean, color="#4f9dff", linewidth=1.0, linestyle=":",
                    alpha=0.8, label=f"mean {gmean:.1f}")
        ax0.set_ylabel("Players online")
        ax0.set_title("Global player count -- all servers combined",
                      fontsize=11)
        ax0.grid(True, alpha=0.3)
        ax0.set_ylim(bottom=0)
        ax0.legend(loc="upper left", fontsize=8, framealpha=0.9, ncol=2)
        glocator = mdates.AutoDateLocator(maxticks=12)
        ax0.xaxis.set_major_locator(glocator)
        ax0.xaxis.set_major_formatter(mdates.ConciseDateFormatter(glocator))
        for lbl in ax0.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

    # --- Player counts over time, one line per server ---
    for i, s in enumerate(line_servers):
        if len(s["history"]) < 2:
            continue
        pts = _downsample_xy(s["history"])
        ax1.plot([t for t, _ in pts], [v for _, v in pts],
                 color=cmap(i % 20), linewidth=1.5,
                 label=f"{s['name'][:26]}  {s['key']}")
    ax1.set_ylabel("Players online")
    ax1.set_title("Player count over time", fontsize=11)
    ax1.grid(True, alpha=0.3)
    ax1.set_ylim(bottom=0)
    if line_servers:
        ax1.legend(loc="upper left", fontsize=8, framealpha=0.9, ncol=2)
        locator = mdates.AutoDateLocator(maxticks=12)
        ax1.xaxis.set_major_locator(locator)
        ax1.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        for lbl in ax1.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

    # --- Uptime ranking ---
    # Rank this panel by the servers' own reliability, not by player counts,
    # so a steady but quiet server is not crowded out by busy unstable ones.
    def _uptime_color(u):
        if u >= 90:
            return "#2e9e5b"   # rock solid
        if u >= 50:
            return "#4f9dff"   # stable
        if u >= 20:
            return "#f0a23a"   # flaky
        return "#e8503a"       # rarely online

    up_rank = sorted(servers, key=lambda s: s["uptime"], reverse=True)[:bar_n]
    up_sorted = sorted(up_rank, key=lambda s: s["uptime"])
    ypos = list(range(len(up_sorted)))
    ax2.barh(ypos, [s["uptime"] for s in up_sorted],
             color=[_uptime_color(s["uptime"]) for s in up_sorted])
    ax2.set_yticks(ypos)
    ax2.set_yticklabels(
        [f"{s['name'][:30]}\n{s['key']}" for s in up_sorted], fontsize=7)
    ax2.set_xlabel("Uptime (% of polls the server was listed)")
    ax2.set_title("Server uptime -- most reliable servers", fontsize=11)
    ax2.set_xlim(0, 108)
    ax2.set_xticks([0, 20, 50, 90, 100])
    ax2.axvline(100, color="#888888", linewidth=0.8, linestyle=":", alpha=0.7)
    ax2.grid(True, axis="x", alpha=0.3)
    for i, s in enumerate(up_sorted):
        u = s["uptime"]
        seen = round(u / 100 * poll_count)
        txt = f"{u:.1f}%  ({seen}/{poll_count})"
        if u >= 38:
            ax2.text(u - 1.5, i, txt, va="center", ha="right",
                     fontsize=7, color="white", fontweight="bold")
        else:
            ax2.text(u + 1.5, i, txt, va="center", ha="left", fontsize=7)
    ax2.legend(handles=[
        Patch(facecolor="#2e9e5b", label="rock solid (>=90%)"),
        Patch(facecolor="#4f9dff", label="stable (50-90%)"),
        Patch(facecolor="#f0a23a", label="flaky (20-50%)"),
        Patch(facecolor="#e8503a", label="rarely online (<20%)"),
    ], loc="lower right", fontsize=7, framealpha=0.9,
        title="reliability", title_fontsize=7)

    # --- Peak vs mean player ranking ---
    pk_sorted = sorted(bar_servers, key=lambda s: s["peak"])
    names = [f"{s['name'][:30]}\n{s['key']}" for s in pk_sorted]
    ypos = range(len(pk_sorted))
    ax3.barh(list(ypos), [s["peak"] for s in pk_sorted], color="#41c97a",
             label="peak players")
    ax3.barh(list(ypos), [s["mean"] for s in pk_sorted], color="#2e9e5b",
             height=0.45, label="mean players")
    ax3.set_yticks(list(ypos))
    ax3.set_yticklabels(names, fontsize=7)
    ax3.set_xlabel("Players")
    ax3.set_title("Peak and mean player count", fontsize=11)
    ax3.grid(True, axis="x", alpha=0.3)
    ax3.legend(loc="lower right", fontsize=8)
    for i, s in enumerate(pk_sorted):
        bot = f"  {s['bot_spikes']} bot" if s["bot_spikes"] else ""
        ax3.text(s["peak"] + 0.5, i, f"{s['peak']}{bot}",
                 va="center", fontsize=7)

    # --- Statistician's summary box ---
    total_peak = sum(s["peak"] for s in servers)
    total_spikes = sum(s["bot_spikes"] for s in servers)
    total_anom = sum(s["anomalies"] for s in servers)
    busiest = ranked[0] if ranked else None
    most_reliable = max(servers, key=lambda s: s["uptime"]) if servers else None
    summary = (f"Range:   {label}\n"
               f"Polls:   {poll_count}\n"
               f"Servers compared: {len(servers)}\n" +
               (f"Global peak players: "
                f"{max(v for _, v in global_history)}\n" if has_global else "") +
               f"Combined per-server peak: {total_peak}\n"
               f"Anomalies: {total_anom}   Bot bursts: {total_spikes}\n" +
               (f"Busiest: {busiest['name'][:34]} (peak {busiest['peak']})\n"
                if busiest else "") +
               (f"Most reliable: {most_reliable['name'][:30]} "
                f"({most_reliable['uptime']:.1f}%)" if most_reliable else ""))
    ax1.text(0.99, 0.97, summary, transform=ax1.transAxes,
             ha="right", va="top", fontsize=8.5, family="monospace",
             bbox={"boxstyle": "round", "facecolor": "#f4f4f4",
                   "edgecolor": "#cccccc", "alpha": 0.9})

    fig.tight_layout(rect=(0, 0, 1, 0.98))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="compare.png")


def _mark_hb_anomalies(ax, anomalies, t0, t1, benign=True,
                       suspicious_marker=True):
    """Draw anomaly markers on an HB axes between t0 and t1.

    Suspicious events (tampering signs) are always bold red. Benign events
    (rollovers, genuine restarts, offline/return) are drawn faint, and only
    when `benign` is True. Returns a counts dict.
    """
    c = {"suspicious": 0, "rollover": 0, "restart": 0, "gone": 0,
         "returned": 0}
    sus_times = []
    for a in anomalies or []:
        try:
            t = _parse(a["ts"])
        except (TypeError, ValueError):
            continue
        if not (t0 <= t <= t1):
            continue
        atype = a["type"]
        if atype in _SUSPICIOUS_TYPES:
            ax.axvline(t, color="#d11a2a", linewidth=2.6, alpha=0.95, zorder=5)
            sus_times.append(t)
            c["suspicious"] += 1
        elif not benign:
            continue
        elif atype in _ROLLOVER_TYPES:
            ax.axvline(t, color="#9b59b6", linewidth=0.8, alpha=0.35,
                       linestyle=":")
            c["rollover"] += 1
        elif atype in _RESTART_TYPES:
            ax.axvline(t, color="#8a97a3", linewidth=0.8, alpha=0.45,
                       linestyle="--")
            c["restart"] += 1
        elif atype in _GONE_TYPES:
            ax.axvline(t, color="#c2c2c2", linewidth=0.8, alpha=0.5,
                       linestyle="--")
            c["gone"] += 1
        elif atype in _RETURN_TYPES:
            ax.axvline(t, color="#41c97a", linewidth=0.9, alpha=0.55)
            c["returned"] += 1
    if suspicious_marker and sus_times:
        # A red triangle pinned to the top of the panel above every
        # suspicious event, so tampering is impossible to miss.
        ax.plot(sus_times, [1.0] * len(sus_times),
                transform=ax.get_xaxis_transform(), marker="v",
                color="#d11a2a", markersize=11, linestyle="none",
                clip_on=False, zorder=6)
    return c


def _flag_suspicious_panel(ax):
    """Tint a subplot red and thicken its border -- a tampered server."""
    ax.set_facecolor("#fceaea")
    for spine in ax.spines.values():
        spine.set_edgecolor("#d11a2a")
        spine.set_linewidth(2.2)


_HB_LEGEND = [
    Line2D([], [], color="#4f9dff", linewidth=1.6, label="HB counter"),
    Patch(facecolor="#4f9dff", alpha=0.20, label="min-max range"),
    Line2D([], [], color="#d11a2a", linewidth=2.6,
           label="SUSPICIOUS - drop / manual reset / impossible jump "
                 "(possible tampering)"),
    Line2D([], [], color="#9b59b6", linewidth=1.2, linestyle=":",
           label="counter rollover (normal 50k wrap)"),
    Line2D([], [], color="#8a97a3", linewidth=1.2, linestyle="--",
           label="genuine server restart (benign)"),
    Line2D([], [], color="#c2c2c2", linewidth=1.2, linestyle="--",
           label="went offline"),
    Line2D([], [], color="#41c97a", linewidth=1.2, label="came back online"),
]


def make_hb_overview_graph(servers, label, page, total_pages):
    """Stacked HB-counter charts for several servers, one row each.

    `servers` is a list of dicts with: name, key, rows (oldest-first snapshot
    rows), anomalies (anomaly rows). Every heartbeat event is marked; servers
    showing tampering signs get a bold red panel so they stand out instantly.
    """
    n = max(len(servers), 1)
    fig = Figure(figsize=(13, 2.9 * n + 1.6))
    axes = fig.subplots(n, 1, squeeze=False)[:, 0]
    fig.suptitle(
        f"Attorney Online -- HB counter overview   "
        f"(page {page}/{total_pages})",
        fontsize=13, fontweight="bold")

    for ax, s in zip(axes, servers):
        rows = s["rows"]
        title = f"{s['name'][:46]}    {s['key']}"
        if len(rows) < 2:
            ax.text(0.5, 0.5, "not enough HB history in this range",
                    transform=ax.transAxes, ha="center", va="center",
                    fontsize=9, color="#888888")
            ax.set_title(title, fontsize=9, loc="left")
            ax.set_yticks([])
            continue

        pts = _downsample(rows)
        times = [p["ts"] for p in pts]

        def _f(v):
            return float("nan") if v is None else float(v)

        hb = [_f(p["hb"]) for p in pts]
        hb_lo = [_f(p["hb_min"]) for p in pts]
        hb_hi = [_f(p["hb_max"]) for p in pts]
        ax.fill_between(times, hb_lo, hb_hi, color="#4f9dff", alpha=0.20,
                        linewidth=0)
        ax.plot(times, hb, color="#4f9dff", linewidth=1.3)
        ax.set_ylabel("HB counter")
        ax.grid(True, alpha=0.3)
        ax.ticklabel_format(axis="y", style="plain", useOffset=False)

        c = _mark_hb_anomalies(ax, s.get("anomalies"), times[0], times[-1])

        cur = rows[-1]["hbcounter"]
        cur_txt = cur if cur is not None else "-"
        if c["suspicious"]:
            _flag_suspicious_panel(ax)
            verdict = (f"   !!!  {c['suspicious']} SUSPICIOUS HB EVENT(S)  "
                       "-- POSSIBLE TAMPERING  !!!")
            tcolor = "#d11a2a"
        else:
            verdict = "   --  HB counter clean, no tampering signs"
            tcolor = "#2e7d32"
        benign = (f"benign:  rollovers {c['rollover']}   "
                  f"genuine restarts {c['restart']}   "
                  f"offline/return {c['gone']}/{c['returned']}")
        ax.set_title(f"{title}\nHB now {cur_txt}{verdict}\n{benign}",
                     fontsize=8.5, loc="left", family="monospace",
                     color=tcolor, fontweight="bold")

        locator = mdates.AutoDateLocator(maxticks=9)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(15)
            lbl.set_horizontalalignment("right")

    axes[-1].set_xlabel("Time (UTC)")
    fig.legend(handles=_HB_LEGEND, loc="lower center", ncol=3, fontsize=8,
               framealpha=0.9)
    fig.tight_layout(rect=(0, 0.05, 1, 0.97))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="hboverview.png")


def make_hb_global_graph(servers, label):
    """Every server's HB counter on one combined axes, tampering flagged red.

    `servers` is a list of dicts with: name, key, rows, anomalies. Only the
    suspicious (tampering) events are marked, as bold red lines, so the wall
    of red shows at a glance when and how often counters were messed with.
    """
    fig = Figure(figsize=(16, 10))
    ax = fig.subplots()
    fig.suptitle("Attorney Online -- HB counter, ALL servers combined",
                 fontsize=14, fontweight="bold")
    cmap = colormaps["tab20"]

    t_min = t_max = None
    plotted = 0
    for s in sorted(servers, key=lambda x: x["name"].lower()):
        rows = s["rows"]
        if len(rows) < 2:
            continue
        pts = _downsample(rows)
        times = [p["ts"] for p in pts]
        hb = [float("nan") if p["hb"] is None else float(p["hb"])
              for p in pts]
        ax.plot(times, hb, color=cmap(plotted % 20), linewidth=1.0,
                alpha=0.85, label=f"{s['name'][:24]}  {s['key']}")
        plotted += 1
        t_min = times[0] if t_min is None else min(t_min, times[0])
        t_max = times[-1] if t_max is None else max(t_max, times[-1])

    sus_servers = []
    total_sus = 0
    if t_min is not None:
        for s in servers:
            c = _mark_hb_anomalies(ax, s.get("anomalies"), t_min, t_max,
                                   benign=False, suspicious_marker=False)
            if c["suspicious"]:
                sus_servers.append((s["name"], s["key"], c["suspicious"]))
                total_sus += c["suspicious"]
    sus_servers.sort(key=lambda x: x[2], reverse=True)

    ax.set_ylabel("HB counter")
    ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.3)
    ax.ticklabel_format(axis="y", style="plain", useOffset=False)
    if plotted:
        ax.legend(loc="upper left", fontsize=6, framealpha=0.9, ncol=3)
        locator = mdates.AutoDateLocator(maxticks=12)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        for lbl in ax.get_xticklabels():
            lbl.set_rotation(20)
            lbl.set_horizontalalignment("right")

    if sus_servers:
        listed = "\n".join(f"  {n} x  {name[:34]}  {key}"
                           for name, key, n in sus_servers[:12])
        more = (f"\n  ...and {len(sus_servers) - 12} more"
                if len(sus_servers) > 12 else "")
        summary = (f"!!!  TAMPERING DETECTED  !!!\n"
                   f"Range: {label}\n"
                   f"{total_sus} suspicious HB event(s) across "
                   f"{len(sus_servers)} server(s):\n{listed}{more}")
        box_color = "#fceaea"
        edge = "#d11a2a"
    else:
        summary = (f"Range: {label}\n"
                   f"{plotted} servers plotted.\n"
                   "No suspicious HB events -- every counter looks clean.")
        box_color = "#eaf6ec"
        edge = "#2e7d32"
    ax.text(0.99, 0.02, summary, transform=ax.transAxes,
            ha="right", va="bottom", fontsize=8, family="monospace",
            fontweight="bold",
            bbox={"boxstyle": "round", "facecolor": box_color,
                  "edgecolor": edge, "linewidth": 1.6, "alpha": 0.95})

    fig.tight_layout(rect=(0, 0, 1, 0.97))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="hbglobal.png")
