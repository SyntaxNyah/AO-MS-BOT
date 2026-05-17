# AO-MS-BOT

A Discord bot that tracks the [Attorney Online master server list](https://servers.aceattorneyonline.com/servers).

It answers `/list` on demand, and in the background it polls the master server
every minute, saving a full snapshot of every server. Over time this builds a
complete historical archive of player counts and heartbeat (HB) counters. The
bot detects anomalies, posts alerts to Discord, and can draw historical graphs.

## Features

- `/list` &mdash; the current server list, on demand
- Automatic polling every minute (configurable)
- A poll summary posted to your events channel after every poll
- Full historical database (SQLite) &mdash; every server, every poll
- Plain-text event log (`data/events.log`) &mdash; a running notepad of everything that happened
- Anomaly detection with alerts posted to a Discord channel
- On-demand historical graphs of HB counter and player counts, with rollovers,
  restarts, counter drops and offline/return events marked on the chart
- Servers are tracked by their `ip:port` address, so two servers sharing a
  display name never collide &mdash; and they can be looked up by address

## How it works

Every minute the bot fetches the master server list and stores one snapshot per
server (name, players, HB counter). The master server itself only refreshes
roughly every 5 minutes, so many consecutive polls record identical values
&mdash; that is intentional, and it means any unexpected change is captured
within a minute. Nothing is ever overwritten, so you keep a full time series for
every server. After each poll the bot posts a short summary (servers online,
players, anomalies) to the events channel.

### Heartbeat counters

Each server reports an ever-rising `hbcounter`. Under normal operation it
increases by about **1 per minute** &mdash; roughly **4&ndash;5 between the
master server's ~5-minute refreshes**. When it reaches **50000** it rolls over
and continues from **49000** &mdash; this is expected behaviour, and the bot
labels it as a normal rollover rather than an anomaly.

### Anomaly detection

On every poll the bot compares each server against its previous snapshot and
flags:

| Type | Meaning |
|------|---------|
| `new_server` | A server appeared on the list for the first time |
| `disappeared` | A server that was listed is no longer present |
| `reappeared` | A previously missing server is back |
| `name_change` | A tracked server changed its name |
| `hb_jump` | The HB counter rose far faster than the ~1/minute rate allows |
| `hb_drop` | The HB counter fell in a way a normal 50k rollover cannot explain |
| `hb_rollover` | The HB counter wrapped normally at 50000 (informational) |
| `hb_restart` | The HB counter reset because the server was restarted |
| `hb_reset` | The HB counter slammed to the floor too fast for a genuine restart &mdash; likely a manual reset |

HB-counter anomalies are posted to their own channel so they are easy to review
separately from routine events. High-severity HB alerts ping `@here`.

### Graphs

`/graph` draws a server's HB counter and player history on one chart. The
server's `ip:port` address is printed on the chart (and in the embed) so
identically named servers stay distinguishable. Every notable event inside the
graphed window is marked on the timeline:

| Marker | Event |
|--------|-------|
| Red solid line | Counter drop &mdash; an unexplained fall or a too-fast manual reset |
| Purple dotted line | Rollover &mdash; the normal counter wrap at 50000 |
| Orange dashed line | Restart &mdash; the counter reset from a server restart |
| Grey dashed line | The server dropped off the master list |
| Green solid line | The server came back onto the master list |

A summary box on the chart counts the rollovers, restarts, drops, and
offline/return cycles over the graphed span.

## Commands

| Command | Description |
|---------|-------------|
| `/list` | Show the current server list |
| `/server <query>` | Details and recent history for one server; `query` is part of a server name or an exact `ip:port` address |
| `/graph <query>` | Historical HB-counter and players graph for a server; `query` is part of a server name or an exact `ip:port` address |
| `/playercount [period] [view]` | Global player-count graph: trend or daily peak/low, filterable by day/week/month/year/all time |
| `/anomalies [count] [alerts_only]` | Show recently detected anomalies |
| `/stats` | Monitoring statistics (polls, snapshots, uptime) |
| `/poll` | Run a master-server poll immediately |

---

# Setup guide (Linux, 24/7)

This walks you through running the bot on a Linux server so it stays up around
the clock. Every step is copy-paste.

## 1. Create the Discord bot

1. Go to <https://discord.com/developers/applications> and click **New Application**.
2. Open the **Bot** tab, click **Reset Token**, and copy the token. Keep it secret.
3. No privileged intents are required &mdash; the bot only uses slash commands.
4. Open **OAuth2 &rarr; URL Generator**. Tick the scopes **`bot`** and
   **`applications.commands`**. Under bot permissions tick **Send Messages**,
   **Embed Links**, and **Attach Files**.
5. Open the generated URL at the bottom and invite the bot to your server.

## 2. Get your channel IDs

1. In Discord: **User Settings &rarr; Advanced &rarr; Developer Mode** &rarr; ON.
2. Right-click the channel for routine events and choose **Copy Channel ID**.
3. Do the same for the channel where you want HB-counter alerts (this can be the
   same channel if you prefer).

## 3. Install on your Linux server

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git
git clone <your-repo-url> AO-MS-BOT
cd AO-MS-BOT
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 4. Configure

```bash
cp .env.example .env
nano .env
```

Fill in `DISCORD_TOKEN`, `EVENTS_CHANNEL_ID`, and `INTEGRITY_CHANNEL_ID`. While
testing, set `GUILD_ID` to your server's ID so slash commands appear instantly.
Save with `Ctrl+O`, `Enter`, then exit with `Ctrl+X`.

## 5. Test run

```bash
source .venv/bin/activate
python bot.py
```

You should see `logged in as ...` and, within a minute, a poll line. Press
`Ctrl+C` to stop the test.

## 6. Run 24/7 with systemd (recommended)

Edit `deploy/ao-ms-bot.service` so `User=` and the paths match your setup, then:

```bash
sudo cp deploy/ao-ms-bot.service /etc/systemd/system/ao-ms-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now ao-ms-bot
```

The bot now starts on boot and restarts automatically if it crashes. Check it:

```bash
systemctl status ao-ms-bot      # is it running?
journalctl -u ao-ms-bot -f      # live logs (Ctrl+C to exit)
```

### Quick alternative: screen

If you do not have root access, use `screen`:

```bash
sudo apt install -y screen
screen -S aobot
source .venv/bin/activate && python bot.py
```

Detach with `Ctrl+A` then `D`. Reattach later with `screen -r aobot`.

## Updating the bot

```bash
cd AO-MS-BOT
git pull
source .venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart ao-ms-bot
```

## Data and logs

Everything lives in the `data/` folder:

- `data/ao_monitor.db` &mdash; SQLite historical database (all snapshots)
- `data/events.log` &mdash; human-readable running log of polls and anomalies
- `data/bot.log` &mdash; bot runtime log

Back up the `data/` folder regularly to preserve your history. Service-level
logs are also available with `journalctl -u ao-ms-bot`.

## Troubleshooting

- **Slash commands not showing up** &mdash; set `GUILD_ID` in `.env` for instant
  registration, or wait up to an hour for global commands to propagate.
- **Bot is offline** &mdash; check `journalctl -u ao-ms-bot -e` for errors; the
  most common cause is a wrong or expired `DISCORD_TOKEN`.
- **No alerts appear** &mdash; confirm the channel IDs are correct and the bot
  can see and post in those channels.
- **`/graph` says not enough history** &mdash; the bot needs at least two polls
  for that server; wait a couple of minutes.
