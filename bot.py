"""AO-MS-BOT -- Attorney Online master server monitor and historical tracker."""
import asyncio
import io
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database as db
import graphs
import monitor
import website

os.makedirs(config.DATA_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(config.BOT_LOG, encoding="utf-8"),
    ],
)
log = logging.getLogger("bot")

START_TIME = datetime.now(timezone.utc)
INTEGRITY_TYPES = {"hb_drop", "hb_jump", "hb_reset"}
BOT_TYPES = {"bot_spike", "bot_spike_end"}
SEV_COLOR = {"alert": 0xE03A3A, "low": 0xE0A53A, "info": 0x3A9DE0}
SEV_EMOJI = {"alert": "\U0001F534", "low": "\U0001F7E1", "info": "\U0001F535"}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


def write_event(line):
    """Append a human-readable line to the running event log (the 'notepad')."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        with open(config.EVENTS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{stamp}] {line}\n")
    except OSError as e:
        log.warning("could not write event log: %s", e)


def _fmt_ts(ts):
    return ts[:16].replace("T", " ") if ts else "-"


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

@bot.event
async def setup_hook():
    db.init_db()
    if config.GUILD_ID:
        guild = discord.Object(id=config.GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
        log.info("slash commands synced to guild %s", config.GUILD_ID)
    else:
        await bot.tree.sync()
        log.info("slash commands synced globally (may take up to 1 hour)")
    poll_loop.start()

    if config.WEBSITE_ENABLED:
        try:
            await website.start()
        except Exception:
            log.exception("could not start the web dashboard")


@bot.event
async def on_ready():
    log.info("logged in as %s (id %s)", bot.user, bot.user.id)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error):
    """Surface command failures instead of leaving the reply spinning forever."""
    log.exception("slash command error: %s", error)
    msg = f"Something went wrong running that command: `{error}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except discord.DiscordException:
        pass


# --------------------------------------------------------------------------
# Background polling
# --------------------------------------------------------------------------

@tasks.loop(minutes=config.POLL_INTERVAL_MINUTES)
async def poll_loop():
    try:
        summary, anomalies = await monitor.run_poll()
    except Exception:
        log.exception("poll failed unexpectedly")
        return

    if summary["ok"]:
        write_event(f"POLL ok -- {summary['count']} servers, "
                    f"{summary['players']} players, "
                    f"{summary['anomalies']} anomalies")
    else:
        write_event(f"POLL FAILED -- {summary.get('error')}")

    await post_poll_summary(summary)

    for a in anomalies:
        write_event(f"  [{a['severity'].upper()}] "
                    f"[{a.get('master') or 'unknown master'}] {a['type']} -- "
                    f"{a['name']} -- {a['detail']}")
        await dispatch_alert(a)


@poll_loop.before_loop
async def before_poll():
    await bot.wait_until_ready()


async def resolve_channel(channel_id):
    """Look up a channel by ID, falling back to an API fetch on cache miss."""
    if not channel_id:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            log.warning("could not resolve channel %s", channel_id)
            return None
    return channel


async def post_poll_summary(summary):
    """Post a summary of every poll to the events channel."""
    channel = await resolve_channel(
        config.EVENTS_CHANNEL_ID or config.INTEGRITY_CHANNEL_ID)
    if channel is None:
        return

    if summary["ok"]:
        embed = discord.Embed(title="Master server poll", color=0x41C97A,
                              timestamp=datetime.now(timezone.utc))
        embed.add_field(name="Servers online", value=str(summary["count"]))
        embed.add_field(name="Players online", value=str(summary["players"]))
        embed.add_field(name="Anomalies this poll",
                        value=str(summary["anomalies"]))
    else:
        embed = discord.Embed(
            title="Master server poll failed",
            description=f"Could not fetch the server list: {summary.get('error')}",
            color=0xE03A3A, timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"Polling every {config.POLL_INTERVAL_MINUTES} min")
    try:
        await channel.send(embed=embed)
    except discord.DiscordException as e:
        log.warning("failed to send poll summary: %s", e)


async def dispatch_alert(a):
    """Send an anomaly to the appropriate Discord channel."""
    integrity = a["type"] in INTEGRITY_TYPES
    is_bot = a["type"] in BOT_TYPES
    # HB-counter faults and bot bursts are routed to the integrity channel.
    important = integrity or a["type"] == "bot_spike"
    primary = (config.INTEGRITY_CHANNEL_ID if important
               else config.EVENTS_CHANNEL_ID)
    channel = await resolve_channel(
        primary or config.EVENTS_CHANNEL_ID or config.INTEGRITY_CHANNEL_ID)
    if channel is None:
        return

    if integrity:
        title = "HB counter anomaly"
    elif is_bot:
        title = "Suspected bot pattern"
    else:
        title = "Server event"
    # Prefix the title with the master server so alerts from the vanilla and
    # Umineko masters stay clearly separate even when they share a channel.
    master = a.get("master")
    title = f"[{master}] {title}" if master else title
    embed = discord.Embed(
        title=f"{title}: {a['type']}",
        description=a["detail"],
        color=SEV_COLOR.get(a["severity"], 0x888888),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Server", value=a["name"][:240], inline=False)
    embed.add_field(name="Address", value=f"`{a['server_key']}`", inline=True)
    embed.add_field(name="Severity", value=a["severity"].upper(), inline=True)
    if master:
        embed.add_field(name="Master server", value=master, inline=True)

    content = ("@here" if (important and a["severity"] == "alert")
               else None)
    try:
        await channel.send(
            content=content, embed=embed,
            allowed_mentions=discord.AllowedMentions(everyone=True))
    except discord.DiscordException as e:
        log.warning("failed to send alert: %s", e)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def resolve_server(query):
    """Return (server_row, None) for a unique match, else (None, message).

    An exact `ip:port` address always resolves uniquely -- so servers that
    share a name can still be picked apart by their address.
    """
    q = query.strip()
    exact = db.get_server(q)
    if exact is not None:
        return exact, None
    matches = db.find_servers(q)
    if not matches:
        return None, f"No tracked server matches `{query}`."
    if len(matches) > 1:
        names = "\n".join(f"- {m['name']} (`{m['server_key']}`)"
                          for m in matches[:15])
        return None, (f"Multiple servers match `{query}`:\n{names}\n"
                      "Please be more specific, or pass the exact address "
                      "shown in brackets.")
    return matches[0], None


# --------------------------------------------------------------------------
# Slash commands
# --------------------------------------------------------------------------

@bot.tree.command(name="list",
                  description="Show the current Attorney Online server list.")
async def list_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    source = "live"
    try:
        servers = await monitor.fetch_servers()
        rows = [(s["name"], s["players"], s["hbcounter"]) for s in servers]
    except Exception:
        source = "last saved poll"
        rows = [(r["name"], r["players"], r["hbcounter"])
                for r in db.last_poll_servers()]

    if not rows:
        await interaction.followup.send("No server data available yet.")
        return

    rows.sort(key=lambda r: (r[1], r[2] or 0), reverse=True)
    lines = []
    for name, players, hb in rows:
        hb_txt = str(hb) if hb is not None else "-"
        lines.append(f"`{players:>2}` players  `HB {hb_txt}`  "
                      f"**{discord.utils.escape_markdown(name)}**")
    desc = "\n".join(lines)
    if len(desc) > 4000:
        desc = desc[:3990] + "\n..."

    embed = discord.Embed(
        title=f"Attorney Online servers ({len(rows)})",
        description=desc, color=0x4F9DFF,
        timestamp=datetime.now(timezone.utc))
    embed.set_footer(text=f"{sum(r[1] for r in rows)} players online  -  "
                          f"source: {source}")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="server",
                  description="Show details and recent history for one server.")
@app_commands.describe(
    query="Part of the server name, or an exact IP:port address")
async def server_cmd(interaction: discord.Interaction, query: str):
    srv, err = resolve_server(query)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    snap = db.latest_snapshot(srv["server_key"])
    anoms = db.server_anomalies(srv["server_key"], 8)

    embed = discord.Embed(title=srv["name"], color=0x4F9DFF,
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Address", value=f"`{srv['server_key']}`")
    embed.add_field(name="Status", value=srv["status"])
    if snap:
        embed.add_field(name="Players", value=str(snap["players"]))
        embed.add_field(name="HB counter",
                        value=str(snap["hbcounter"]
                                  if snap["hbcounter"] is not None else "-"))
        embed.add_field(name="Last snapshot", value=_fmt_ts(snap["ts"]))
    embed.add_field(name="First seen", value=_fmt_ts(srv["first_seen"]))
    embed.add_field(name="Last seen", value=_fmt_ts(srv["last_seen"]))
    webao = config.webao_url(srv["ip"], srv["ws_port"], srv["wss_port"],
                             srv["name"])
    embed.add_field(
        name="Join in browser",
        value=(f"[Open in WebAO]({webao})" if webao
               else "This server does not publish a WebAO port."),
        inline=False)
    if anoms:
        a_lines = "\n".join(
            f"- `{_fmt_ts(a['ts'])}` {a['type']} -- {a['detail']}"
            for a in anoms)
        embed.add_field(name="Recent anomalies", value=a_lines[:1000],
                        inline=False)
    embed.set_footer(text="Use /graph for a historical chart")
    await interaction.response.send_message(embed=embed)


_GRAPH_PERIODS = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def _resolve_since(now, period, days, weeks, start_date):
    """Turn the graph time-filter options into (since_iso, label, error).

    Precedence: explicit start date > custom days/weeks > preset period.
    `since_iso` and `error` are None when not applicable.
    """
    if start_date:
        try:
            d = date.fromisoformat(start_date.strip())
        except ValueError:
            return None, None, (f"`{start_date}` is not a valid date "
                                "-- use YYYY-MM-DD.")
        since = datetime(d.year, d.month, d.day,
                         tzinfo=timezone.utc).isoformat()
        return since, f"since {d.isoformat()}", None
    if days or weeks:
        since = (now - timedelta(days=days or 0, weeks=weeks or 0)).isoformat()
        parts = []
        if weeks:
            parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        return since, "last " + " ".join(parts), None
    if period and period.value in _GRAPH_PERIODS:
        return ((now - _GRAPH_PERIODS[period.value]).isoformat(),
                period.name, None)
    return None, period.name if period else "all time", None


@bot.tree.command(
    name="graph",
    description="Historical HB-counter and players graph for a server.")
@app_commands.describe(
    query="Part of the server name, or an exact IP:port address",
    period="How far back to graph (default: all history)",
    days="Custom: graph this many days back",
    weeks="Custom: graph this many weeks back",
    start_date="Graph everything since this date (YYYY-MM-DD)")
@app_commands.choices(period=[
    app_commands.Choice(name="Last day", value="day"),
    app_commands.Choice(name="Last week", value="week"),
    app_commands.Choice(name="Last month", value="month"),
    app_commands.Choice(name="Last year", value="year"),
    app_commands.Choice(name="All time", value="all"),
])
async def graph_cmd(interaction: discord.Interaction, query: str,
                    period: app_commands.Choice[str] = None,
                    days: app_commands.Range[int, 1, None] = None,
                    weeks: app_commands.Range[int, 1, None] = None,
                    start_date: str = None):
    srv, err = resolve_server(query)
    if err:
        await interaction.response.send_message(err, ephemeral=True)
        return

    await interaction.response.defer()
    now = datetime.now(timezone.utc)
    since, label, err = _resolve_since(now, period, days, weeks, start_date)
    if err:
        await interaction.followup.send(err)
        return
    rows = db.server_history(srv["server_key"], since=since)
    if len(rows) < 2:
        await interaction.followup.send(
            f"Not enough history for **{srv['name']}** in {label} "
            "-- need at least 2 polls.")
        return

    anomalies = db.server_anomalies(srv["server_key"], limit=5000)
    img = await asyncio.to_thread(
        graphs.make_hb_graph, srv["name"], rows, anomalies,
        srv["server_key"])
    embed = discord.Embed(
        title=f"{srv['name']} -- history",
        description=f"`{srv['server_key']}`\n{len(rows)} data points ({label})",
        color=0x4F9DFF)
    embed.set_image(url="attachment://history.png")
    await interaction.followup.send(embed=embed, file=img)


@bot.tree.command(
    name="playercount",
    description="Graph the global Attorney Online player count over time.")
@app_commands.describe(
    period="How far back to graph (default: all history)",
    view="Continuous trend, or per-day peak/low breakdown",
    days="Custom: graph this many days back",
    weeks="Custom: graph this many weeks back",
    start_date="Graph everything since this date (YYYY-MM-DD)")
@app_commands.choices(
    period=[
        app_commands.Choice(name="Last day", value="day"),
        app_commands.Choice(name="Last week", value="week"),
        app_commands.Choice(name="Last month", value="month"),
        app_commands.Choice(name="Last year", value="year"),
        app_commands.Choice(name="All time", value="all"),
    ],
    view=[
        app_commands.Choice(name="Trend (continuous)", value="trend"),
        app_commands.Choice(name="Daily peak / low", value="daily"),
    ])
async def playercount_cmd(interaction: discord.Interaction,
                          period: app_commands.Choice[str] = None,
                          view: app_commands.Choice[str] = None,
                          days: app_commands.Range[int, 1, None] = None,
                          weeks: app_commands.Range[int, 1, None] = None,
                          start_date: str = None):
    await interaction.response.defer()
    now = datetime.now(timezone.utc)
    since, label, err = _resolve_since(now, period, days, weeks, start_date)
    if err:
        await interaction.followup.send(err)
        return

    rows = [r for r in db.poll_history(since=since)
            if r["player_count"] is not None]
    if len(rows) < 2:
        await interaction.followup.send(
            f"Not enough global player-count history in {label} "
            "-- need at least 2 polls.")
        return

    view_value = view.value if view else "trend"
    img = await asyncio.to_thread(
        graphs.make_player_graph, rows, label, view_value)
    kind = "daily peak / low" if view_value == "daily" else "trend"
    embed = discord.Embed(
        title="Attorney Online -- global player count",
        description=f"{len(rows)} polls ({label}) -- {kind}",
        color=0x41C97A)
    embed.set_image(url="attachment://playercount.png")
    await interaction.followup.send(embed=embed, file=img)


# Reliability tiers for /compare -- every server falls into exactly one,
# matched first-to-last so the boundary values land in the higher tier.
_RELIABILITY_TIERS = [
    ("Rock solid (>=90%)", 90.0, 100.0),
    ("Stable (50-90%)", 50.0, 90.0),
    ("Flaky (20-50%)", 20.0, 50.0),
    ("Rarely online (<20%)", 0.0, 20.0),
]


def _build_tier_roster(by_tier, label, poll_count, total):
    """Render the full per-server reliability roster as a plain-text file."""
    lines = [
        "Attorney Online -- Ultimate server comparison",
        "All-server reliability tiers",
        f"Range: {label}",
        f"{poll_count} polls compared across {total} servers.",
        "",
    ]
    for tname, _, _ in _RELIABILITY_TIERS:
        members = sorted(by_tier[tname],
                         key=lambda s: (s["uptime"], s["peak"]), reverse=True)
        lines.append(f"== {tname} -- {len(members)} server(s) ==")
        for s in members:
            bot = f"   bot bursts: {s['bot_spikes']}" if s["bot_spikes"] else ""
            lines.append(
                f"  {s['uptime']:>6.1f}%  {s['name'][:34]:<34}  "
                f"{s['key']:<24}  peak {s['peak']:>4}  "
                f"mean {s['mean']:>6.1f}{bot}")
        lines.append("")
    data = "\n".join(lines).encode("utf-8")
    return discord.File(io.BytesIO(data), filename="server_tiers.txt")


@bot.tree.command(
    name="compare",
    description="Ultimate statistician: compare every server's stats together.")
@app_commands.describe(
    period="How far back to compare (default: all history)",
    days="Custom: compare this many days back",
    weeks="Custom: compare this many weeks back",
    start_date="Compare everything since this date (YYYY-MM-DD)")
@app_commands.choices(period=[
    app_commands.Choice(name="Last day", value="day"),
    app_commands.Choice(name="Last week", value="week"),
    app_commands.Choice(name="Last month", value="month"),
    app_commands.Choice(name="Last year", value="year"),
    app_commands.Choice(name="All time", value="all"),
])
async def compare_cmd(interaction: discord.Interaction,
                      period: app_commands.Choice[str] = None,
                      days: app_commands.Range[int, 1, None] = None,
                      weeks: app_commands.Range[int, 1, None] = None,
                      start_date: str = None):
    await interaction.response.defer()
    now = datetime.now(timezone.utc)
    since, label, err = _resolve_since(now, period, days, weeks, start_date)
    if err:
        await interaction.followup.send(err)
        return

    polls = await asyncio.to_thread(db.poll_history, since=since)
    poll_count = len(polls)
    if poll_count < 2:
        await interaction.followup.send(
            f"Not enough poll history in {label} -- need at least 2 polls.")
        return

    global_history = [(datetime.fromisoformat(r["ts"]), r["player_count"])
                      for r in polls if r["player_count"] is not None]

    # Per-server aggregates are computed in SQL -- the full snapshot table is
    # never loaded into memory, which keeps /compare fast even after years of
    # history. Only the few servers actually plotted need their time series.
    stats_rows = await asyncio.to_thread(db.server_stats, since=since)
    if not stats_rows:
        await interaction.followup.send(f"No server data recorded in {label}.")
        return
    counts = await asyncio.to_thread(db.anomaly_counts, since=since)
    name_map = {s["server_key"]: s["name"] for s in db.all_servers()}

    servers = []
    for r in stats_rows:
        key = r["server_key"]
        c = counts.get(key, {})
        servers.append({
            "name": name_map.get(key, key),
            "key": key,
            "history": [],
            "peak": r["peak"] or 0,
            "mean": r["mean"] or 0.0,
            "uptime": min(100.0 * r["snaps"] / poll_count, 100.0),
            "anomalies": c.get("total", 0),
            "alerts": c.get("alerts", 0),
            "bot_spikes": c.get("bot_spikes", 0),
        })

    # Only the top servers by peak appear as time-series lines, so fetch the
    # heavier snapshot history for those alone.
    plotted = sorted(servers, key=lambda s: (s["peak"], s["mean"]),
                     reverse=True)[:12]
    for s in plotted:
        hist = await asyncio.to_thread(
            db.server_history, s["key"], since=since)
        s["history"] = [(datetime.fromisoformat(h["ts"]), h["players"])
                         for h in hist]

    img = await asyncio.to_thread(
        graphs.make_compare_graph, servers, label, poll_count,
        global_history)

    # Group every server into a reliability tier so none is left unranked,
    # then list them tier by tier (busiest first within each tier).
    by_tier = {name: [] for name, _, _ in _RELIABILITY_TIERS}
    for s in servers:
        for tname, lo, hi in _RELIABILITY_TIERS:
            if lo <= s["uptime"] <= hi:
                by_tier[tname].append(s)
                break

    header = (f"`{poll_count}` polls compared across `{len(servers)}` "
              f"servers ({label}). Every server is grouped into a "
              "reliability tier below.")

    sections = []
    for tname, _, _ in _RELIABILITY_TIERS:
        members = sorted(by_tier[tname], key=lambda s: (s["uptime"], s["peak"]),
                         reverse=True)
        sections.append(f"\n__{tname}__ -- {len(members)} server(s)")
        for s in members:
            name = discord.utils.escape_markdown(s["name"][:34])
            bot = f"  bot:`{s['bot_spikes']}`" if s["bot_spikes"] else ""
            sections.append(
                f"`{s['uptime']:>5.1f}%` **{name}** `{s['key']}` -- "
                f"peak `{s['peak']}` mean `{s['mean']:.1f}`{bot}")

    desc = header + "\n" + "\n".join(sections)
    files = [img]
    if len(desc) > 4000:
        # Too many servers for one embed -- attach the full roster as a file
        # so every server is still listed somewhere.
        counts = "  |  ".join(
            f"{tname.split(' (')[0]}: {len(by_tier[tname])}"
            for tname, _, _ in _RELIABILITY_TIERS)
        desc = header + "\n\n" + counts + ("\n\nFull per-server roster "
               "attached as `server_tiers.txt`.")
        files.append(_build_tier_roster(by_tier, label, poll_count,
                                        len(servers)))

    embed = discord.Embed(
        title="Ultimate server comparison",
        description=desc, color=0x9B59B6,
        timestamp=datetime.now(timezone.utc))
    embed.set_image(url="attachment://compare.png")
    embed.set_footer(
        text="Tiered by uptime  -  bot: suspected bot bursts")
    await interaction.followup.send(embed=embed, files=files)


# How many servers share one page of the /hblist overview graph.
_HB_PER_PAGE = 6


class HBListView(discord.ui.View):
    """Paged HB-counter overview -- one graph page per group of servers.

    The server roster and time window are held on the view; each page fetches
    only its own servers' history so the graph stays quick no matter how many
    servers are tracked.
    """

    def __init__(self, items, label, since):
        super().__init__(timeout=600)
        self.items = items                 # ordered list of (key, name)
        self.label = label
        self.since = since
        self.page = 0
        self.total_pages = max(
            1, (len(items) + _HB_PER_PAGE - 1) // _HB_PER_PAGE)
        self.message = None
        self._sync_buttons()

    def _sync_buttons(self):
        for child in self.children:
            if child.custom_id == "hb_prev":
                child.disabled = self.page <= 0
            elif child.custom_id == "hb_next":
                child.disabled = self.page >= self.total_pages - 1

    async def render(self):
        """Build the embed and graph for the current page."""
        start = self.page * _HB_PER_PAGE
        page_items = self.items[start:start + _HB_PER_PAGE]
        servers = []
        for key, name in page_items:
            rows = await asyncio.to_thread(
                db.server_history, key, since=self.since)
            anoms = await asyncio.to_thread(db.server_anomalies, key, 5000)
            servers.append({"name": name, "key": key,
                            "rows": rows, "anomalies": anoms})

        img = await asyncio.to_thread(
            graphs.make_hb_overview_graph, servers, self.label,
            self.page + 1, self.total_pages)

        lines = []
        for s in servers:
            latest = s["rows"][-1] if s["rows"] else None
            hb = (latest["hbcounter"] if latest
                  and latest["hbcounter"] is not None else "-")
            name = discord.utils.escape_markdown(s["name"][:40])
            sus = sum(1 for a in s["anomalies"]
                      if a["type"] in INTEGRITY_TYPES)
            tag = (f"  -- **{sus} SUSPICIOUS (possible tampering)**"
                   if sus else "  -- clean")
            lines.append(f"`HB {hb}`  **{name}**  `{s['key']}`{tag}")
        embed = discord.Embed(
            title="HB counter overview -- all servers",
            description=(
                f"Heartbeat tracking for every server ({self.label}).\n"
                f"Page **{self.page + 1}/{self.total_pages}** -- "
                f"{len(self.items)} servers total.\n\n" + "\n".join(lines)),
            color=0x4F9DFF, timestamp=datetime.now(timezone.utc))
        embed.set_image(url="attachment://hboverview.png")
        embed.set_footer(text="Use the buttons to page through every server")
        return embed, img

    async def _show(self, interaction):
        await interaction.response.defer()
        self._sync_buttons()
        embed, img = await self.render()
        await interaction.edit_original_response(
            embed=embed, attachments=[img], view=self)

    @discord.ui.button(label="Prev", custom_id="hb_prev",
                       style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button):
        if self.page > 0:
            self.page -= 1
        await self._show(interaction)

    @discord.ui.button(label="Next", custom_id="hb_next",
                       style=discord.ButtonStyle.primary)
    async def next_btn(self, interaction: discord.Interaction, button):
        if self.page < self.total_pages - 1:
            self.page += 1
        await self._show(interaction)

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.DiscordException:
                pass


async def _hblist_global(interaction, ordered, label, since):
    """Render every server's HB counter on one combined graph."""
    data = []
    for s in ordered:
        key = s["server_key"]
        rows = await asyncio.to_thread(db.server_history, key, since=since)
        anoms = await asyncio.to_thread(db.server_anomalies, key, 5000)
        data.append({"name": s["name"], "key": key,
                     "rows": rows, "anomalies": anoms})

    img = await asyncio.to_thread(graphs.make_hb_global_graph, data, label)

    sus_lines = []
    clean = 0
    for s in data:
        sus = sum(1 for a in s["anomalies"] if a["type"] in INTEGRITY_TYPES)
        if sus:
            name = discord.utils.escape_markdown(s["name"][:40])
            sus_lines.append(f"`{sus}x`  **{name}**  `{s['key']}`")
        else:
            clean += 1

    desc = (f"Every server's HB counter on one graph ({label}).\n"
            f"**{len(data)}** servers -- {clean} clean, "
            f"{len(sus_lines)} with suspicious activity.\n\n")
    if sus_lines:
        desc += ("**Possible tampering detected:**\n"
                 + "\n".join(sus_lines[:30]))
    else:
        desc += "No suspicious HB events -- every counter looks clean."
    embed = discord.Embed(
        title="HB counter -- ALL servers combined",
        description=desc[:4000],
        color=0xD11A2A if sus_lines else 0x2E7D32,
        timestamp=datetime.now(timezone.utc))
    embed.set_image(url="attachment://hbglobal.png")
    await interaction.followup.send(embed=embed, file=img)


@bot.tree.command(
    name="hblist",
    description="HB-counter overview for every server, tampering flagged red.")
@app_commands.describe(
    view="Paged (a few servers per page) or Global (all on one graph)",
    period="How far back to graph (default: all history)",
    days="Custom: graph this many days back",
    weeks="Custom: graph this many weeks back",
    start_date="Graph everything since this date (YYYY-MM-DD)")
@app_commands.choices(
    view=[
        app_commands.Choice(name="Paged (scroll with buttons)",
                            value="paged"),
        app_commands.Choice(name="Global (every server on one graph)",
                            value="global"),
    ],
    period=[
        app_commands.Choice(name="Last day", value="day"),
        app_commands.Choice(name="Last week", value="week"),
        app_commands.Choice(name="Last month", value="month"),
        app_commands.Choice(name="Last year", value="year"),
        app_commands.Choice(name="All time", value="all"),
    ])
async def hblist_cmd(interaction: discord.Interaction,
                     view: app_commands.Choice[str] = None,
                     period: app_commands.Choice[str] = None,
                     days: app_commands.Range[int, 1, None] = None,
                     weeks: app_commands.Range[int, 1, None] = None,
                     start_date: str = None):
    await interaction.response.defer()
    now = datetime.now(timezone.utc)
    since, label, err = _resolve_since(now, period, days, weeks, start_date)
    if err:
        await interaction.followup.send(err)
        return

    servers = await asyncio.to_thread(db.all_servers)
    if not servers:
        await interaction.followup.send("No servers are being tracked yet.")
        return

    # Servers with the most logged anomalies come first, so the noisy ones
    # land on the opening page / lead the listing.
    counts = await asyncio.to_thread(db.anomaly_counts, since=since)
    ordered = sorted(
        servers,
        key=lambda s: (counts.get(s["server_key"], {}).get("total", 0),
                       s["name"]),
        reverse=True)

    if view is not None and view.value == "global":
        await _hblist_global(interaction, ordered, label, since)
        return

    items = [(s["server_key"], s["name"]) for s in ordered]
    hb_view = HBListView(items, label, since)
    embed, img = await hb_view.render()
    await interaction.followup.send(embed=embed, file=img, view=hb_view)
    hb_view.message = await interaction.original_response()


@bot.tree.command(name="anomalies",
                  description="Show recently detected anomalies.")
@app_commands.describe(count="How many to show (1-25, default 10)",
                       alerts_only="Only show high-severity alerts")
async def anomalies_cmd(interaction: discord.Interaction,
                        count: int = 10, alerts_only: bool = False):
    count = max(1, min(count, 25))
    rows = db.recent_anomalies(count, alerts_only)
    if not rows:
        await interaction.response.send_message("No anomalies recorded yet.")
        return

    lines = []
    for r in rows:
        lines.append(
            f"{SEV_EMOJI.get(r['severity'], '-')} `{_fmt_ts(r['ts'])}` "
            f"**{r['type']}** -- {discord.utils.escape_markdown(r['name'])}\n"
            f"    {r['detail']}")
    embed = discord.Embed(title="Recent anomalies",
                          description="\n".join(lines)[:4000],
                          color=0xE0A53A,
                          timestamp=datetime.now(timezone.utc))
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="stats", description="Show monitoring statistics.")
async def stats_cmd(interaction: discord.Interaction):
    s = db.stats()
    uptime = str(datetime.now(timezone.utc) - START_TIME).split(".")[0]
    embed = discord.Embed(title="Monitoring stats", color=0x41C97A,
                          timestamp=datetime.now(timezone.utc))
    embed.add_field(name="Successful polls", value=str(s["polls"]))
    embed.add_field(name="Failed polls", value=str(s["failed"]))
    embed.add_field(name="Snapshots stored", value=str(s["snapshots"]))
    embed.add_field(name="Servers known", value=str(s["known"]))
    embed.add_field(name="Online now", value=str(s["online"]))
    embed.add_field(name="Anomalies logged",
                    value=f"{s['anomalies']} ({s['alerts']} alerts)")
    embed.add_field(name="Suspected bot bursts", value=str(s["bot_spikes"]))
    embed.add_field(name="Last poll", value=_fmt_ts(s["last_poll"]),
                    inline=False)
    embed.add_field(name="Bot uptime", value=uptime, inline=False)
    embed.set_footer(text=f"Polling every {config.POLL_INTERVAL_MINUTES} min")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="deadservers",
    description="List servers absent from the master list long enough to be "
                "considered shut down.")
async def deadservers_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=config.DEAD_SERVER_DAYS)).isoformat()
    rows = db.dead_servers(cutoff)
    if not rows:
        await interaction.followup.send(
            "No dead servers -- every tracked server has appeared on the "
            f"master list within the last {config.DEAD_SERVER_DAYS} days.")
        return

    lines = []
    for r in rows:
        days = (now - datetime.fromisoformat(r["last_seen"])).days
        name = discord.utils.escape_markdown(r["name"])
        lines.append(f"`{days:>4}d`  **{name}**  `{r['server_key']}`\n"
                      f"    last seen {_fmt_ts(r['last_seen'])}")
    desc = "\n".join(lines)
    if len(desc) > 3900:
        desc = desc[:3890] + "\n..."

    embed = discord.Embed(
        title=f"Dead servers ({len(rows)})",
        description=desc, color=0x7A7A7A, timestamp=now)
    embed.set_footer(
        text=f"Shut down = absent for {config.DEAD_SERVER_DAYS}+ days  -  "
             "history is kept; a server that returns drops off this list")
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="poll",
                  description="Run a master-server poll right now.")
async def poll_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    summary, anomalies = await monitor.run_poll()
    for a in anomalies:
        write_event(f"  [{a['severity'].upper()}] "
                    f"[{a.get('master') or 'unknown master'}] {a['type']} -- "
                    f"{a['name']} -- {a['detail']}")
        await dispatch_alert(a)
    if summary["ok"]:
        msg = (f"Poll complete -- {summary['count']} servers, "
               f"{summary['players']} players, "
               f"{summary['anomalies']} anomalies detected.")
    else:
        msg = f"Poll failed: {summary.get('error')}"
    await interaction.followup.send(msg)


def main():
    if not config.DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN is not set. "
              "Copy .env.example to .env and fill it in.")
        sys.exit(1)
    bot.run(config.DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
