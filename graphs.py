"""Renders historical graphs for a single server.

The chart adapts to any time span -- from a few minutes up to years of
snapshots -- by collapsing dense minute-level data into time buckets that keep
the min/max range, so restarts and spikes survive the downsampling.
"""
import io
from datetime import datetime

import discord
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch

# Anomalies worth marking on the chart: a counter that moved backwards (the
# server lost its heartbeat count) or a drop big enough to look like tampering.
_DROP_TYPES = {"hb_drop"}
_RESTART_TYPES = {"hb_restart", "hb_rollover"}

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


def make_hb_graph(name, rows, anomalies=None):
    """Build a PNG with HB counter and player history. `rows` is oldest-first.

    `anomalies` is an optional list of anomaly rows; counter drops and restarts
    inside the graphed window are marked on the HB axis.
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

    # --- HB counter ---
    ax1.fill_between(times, hb_lo, hb_hi, color="#4f9dff", alpha=0.22,
                     linewidth=0)
    ax1.plot(times, hb, color="#4f9dff", linewidth=1.4)
    ax1.set_ylabel("HB counter")
    ax1.grid(True, alpha=0.3)

    drops = restarts = 0
    if anomalies:
        for a in anomalies:
            try:
                t = _parse(a["ts"])
            except (TypeError, ValueError):
                continue
            if not (times[0] <= t <= times[-1]):
                continue
            if a["type"] in _DROP_TYPES:
                ax1.axvline(t, color="#e8503a", linewidth=1.0, alpha=0.7)
                drops += 1
            elif a["type"] in _RESTART_TYPES:
                ax1.axvline(t, color="#f0a23a", linewidth=0.9, alpha=0.55,
                            linestyle="--")
                restarts += 1

    legend = [
        Line2D([], [], color="#4f9dff", linewidth=1.6, label="HB counter"),
        Patch(facecolor="#4f9dff", alpha=0.22, label="min-max range"),
    ]
    if drops:
        legend.append(Line2D([], [], color="#e8503a", linewidth=1.0,
                              label=f"drop >35 ({drops})"))
    if restarts:
        legend.append(Line2D([], [], color="#f0a23a", linewidth=1.0,
                              linestyle="--", label=f"restart / reset ({restarts})"))
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
    summary = (f"Span: {span}\n"
               f"Snapshots: {len(rows)}  (plotted {len(pts)})\n"
               f"Restarts: {restarts}   Drops >35: {drops}\n"
               f"Players  peak {peak} / mean {mean:.1f}")
    ax1.text(0.99, 0.97, summary, transform=ax1.transAxes,
             ha="right", va="top", fontsize=8, family="monospace",
             bbox={"boxstyle": "round", "facecolor": "#f4f4f4",
                   "edgecolor": "#cccccc", "alpha": 0.9})

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="history.png")
