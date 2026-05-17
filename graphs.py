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
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# Anomalies worth marking on the chart, grouped by how they're drawn:
#   drops     -- a counter fall nothing can explain, or a too-fast manual reset
#   rollovers -- the normal counter wrap at HB_CAP
#   restarts  -- the counter reset because the server was restarted
#   gone      -- the server dropped off the master list
#   returned  -- the server came back onto the master list
_DROP_TYPES = {"hb_drop", "hb_reset"}
_ROLLOVER_TYPES = {"hb_rollover"}
_RESTART_TYPES = {"hb_restart"}
_GONE_TYPES = {"disappeared"}
_RETURN_TYPES = {"reappeared"}

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

    drops = rollovers = restarts = gone = returned = 0
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
