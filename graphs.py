"""Renders historical graphs for a single server."""
import io
from datetime import datetime

import discord
import matplotlib.dates as mdates
from matplotlib.figure import Figure


def make_hb_graph(name, rows):
    """Build a PNG with HB counter and player history. `rows` is oldest-first."""
    times = [datetime.fromisoformat(r["ts"]) for r in rows]
    hb = [r["hbcounter"] if r["hbcounter"] is not None else float("nan")
          for r in rows]
    players = [r["players"] for r in rows]

    fig = Figure(figsize=(10, 7))
    ax1, ax2 = fig.subplots(2, 1, sharex=True)
    fig.suptitle(f"{name} -- historical tracking", fontsize=13, fontweight="bold")

    ax1.plot(times, hb, color="#4f9dff", linewidth=1.6)
    ax1.set_ylabel("HB counter")
    ax1.grid(True, alpha=0.3)

    ax2.plot(times, players, color="#41c97a", linewidth=1.6)
    ax2.set_ylabel("Players")
    ax2.set_xlabel("Time (UTC)")
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    for label in ax2.get_xticklabels():
        label.set_rotation(30)
        label.set_horizontalalignment("right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    buf.seek(0)
    return discord.File(buf, filename="history.png")
