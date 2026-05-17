"""AO-MS-BOT -- Attorney Online master server monitor and historical tracker."""
import asyncio
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
INTEGRITY_TYPES = {"hb_drop", "hb_jump"}
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


@bot.event
async def on_ready():
    log.info("logged in as %s (id %s)", bot.user, bot.user.id)


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
        write_event(f"  [{a['severity'].upper()}] {a['type']} -- "
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
    primary = config.INTEGRITY_CHANNEL_ID if integrity else config.EVENTS_CHANNEL_ID
    channel = await resolve_channel(
        primary or config.EVENTS_CHANNEL_ID or config.INTEGRITY_CHANNEL_ID)
    if channel is None:
        return

    title = "HB counter anomaly" if integrity else "Server event"
    embed = discord.Embed(
        title=f"{title}: {a['type']}",
        description=a["detail"],
        color=SEV_COLOR.get(a["severity"], 0x888888),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="Server", value=a["name"][:240], inline=False)
    embed.add_field(name="Address", value=f"`{a['server_key']}`", inline=True)
    embed.add_field(name="Severity", value=a["severity"].upper(), inline=True)

    content = "@here" if (integrity and a["severity"] == "alert") else None
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
    """Return (server_row, None) for a unique match, else (None, message)."""
    matches = db.find_servers(query)
    if not matches:
        return None, f"No tracked server matches `{query}`."
    if len(matches) > 1:
        names = "\n".join(f"- {m['name']} (`{m['server_key']}`)"
                          for m in matches[:15])
        return None, (f"Multiple servers match `{query}`:\n{names}\n"
                      "Please be more specific.")
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
@app_commands.describe(query="Part of the server name")
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


@bot.tree.command(
    name="graph",
    description="Historical HB-counter and players graph for a server.")
@app_commands.describe(
    query="Part of the server name",
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
    since = None
    # Precedence: explicit start date > custom days/weeks > preset period.
    if start_date:
        try:
            d = date.fromisoformat(start_date.strip())
        except ValueError:
            await interaction.followup.send(
                f"`{start_date}` is not a valid date -- use YYYY-MM-DD.")
            return
        since = datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()
        label = f"since {d.isoformat()}"
    elif days or weeks:
        since = (now - timedelta(days=days or 0, weeks=weeks or 0)).isoformat()
        parts = []
        if weeks:
            parts.append(f"{weeks} week{'s' if weeks != 1 else ''}")
        if days:
            parts.append(f"{days} day{'s' if days != 1 else ''}")
        label = "last " + " ".join(parts)
    elif period and period.value in _GRAPH_PERIODS:
        since = (now - _GRAPH_PERIODS[period.value]).isoformat()
        label = period.name
    else:
        label = period.name if period else "all time"
    rows = db.server_history(srv["server_key"], since=since)
    if len(rows) < 2:
        await interaction.followup.send(
            f"Not enough history for **{srv['name']}** in {label} "
            "-- need at least 2 polls.")
        return

    anomalies = db.server_anomalies(srv["server_key"], limit=5000)
    img = await asyncio.to_thread(
        graphs.make_hb_graph, srv["name"], rows, anomalies)
    embed = discord.Embed(
        title=f"{srv['name']} -- history",
        description=f"{len(rows)} data points ({label})",
        color=0x4F9DFF)
    embed.set_image(url="attachment://history.png")
    await interaction.followup.send(embed=embed, file=img)


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
    embed.add_field(name="Last poll", value=_fmt_ts(s["last_poll"]),
                    inline=False)
    embed.add_field(name="Bot uptime", value=uptime, inline=False)
    embed.set_footer(text=f"Polling every {config.POLL_INTERVAL_MINUTES} min")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="poll",
                  description="Run a master-server poll right now.")
async def poll_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    summary, anomalies = await monitor.run_poll()
    for a in anomalies:
        write_event(f"  [{a['severity'].upper()}] {a['type']} -- "
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
