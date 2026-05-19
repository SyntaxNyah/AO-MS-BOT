# AO-MS-BOT

A Discord bot that tracks the [Attorney Online master server list](https://servers.aceattorneyonline.com/servers) and the [Umineko Online AO master server list](https://servers.umineko.online/servers).

It answers `/list` on demand, and in the background it polls the master server
every minute, saving a full snapshot of every server. Over time this builds a
complete historical archive of player counts and heartbeat (HB) counters. The
bot detects anomalies, posts alerts to Discord, and can draw historical graphs.

## Features

- `/list` &mdash; the current server list, on demand
- Automatic polling every minute (configurable)
- Polls **two master servers by default** &mdash; the official Attorney Online
  list and our open-source master server &mdash; or any number you configure;
  the lists are merged, a failing master never blacks out the others, each
  master's stats are kept separate, and the dashboard can filter by master
- A poll summary posted to your events channel after every poll
- Full historical database (SQLite) &mdash; every server, every poll
- Plain-text event log (`data/events.log`) &mdash; a running notepad of everything that happened
- Anomaly detection with alerts posted to a Discord channel
- Bot-pattern detection &mdash; flags near-empty servers that suddenly fill with
  players over a poll or two, and logs when the burst subsides
- On-demand historical graphs of HB counter and player counts, with rollovers,
  restarts, counter drops, bot bursts and offline/return events marked
- `/compare` &mdash; an all-server "Ultimate statistician" comparison of player
  counts, uptime and peak/mean stats
- Servers are tracked by their `ip:port` address, so two servers sharing a
  display name never collide &mdash; and they can be looked up by address
- **WebAO join links** &mdash; every server that publishes a websocket port
  gets a one-click "join in browser" link to the WebAO client, in `/server`,
  on the dashboard server tables and in each server's detail view
- Optional live **web dashboard** &mdash; flip one config switch and the bot
  also serves a full browser portal: every server, every graph, player-count
  trends, daily breakdowns, server comparisons, HB-counter tampering and a
  filterable anomaly browser, all updating live

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
| `hb_jump` | The HB counter leapt up well beyond the slack for a normal master-server refresh &mdash; low severity, since an upward jump is rarely a fault |
| `hb_drop` | The HB counter fell in a way a normal 50k rollover cannot explain |
| `hb_rollover` | The HB counter wrapped normally at 50000 (informational) |
| `hb_restart` | The HB counter reset because the server was restarted |
| `hb_reset` | The HB counter slammed to the floor too fast for a genuine restart &mdash; likely a manual reset |
| `bot_spike` | A near-empty server suddenly filled with players &mdash; a suspected automated bot fill |
| `bot_spike_end` | A suspected bot burst subsided and the player count fell back to normal |

HB-counter anomalies and suspected bot bursts are posted to the integrity
channel so they are easy to review separately from routine events.
High-severity integrity alerts (including bot bursts) ping `@here`.

### Bot-pattern detection

Bot fills look nothing like real player activity, and the bot exploits every
one of those tells. The goal is two-fold:

1. **Let servers grow organically without false flags.** A popular show
   filling a room over several minutes is not a bot.
2. **Catch the obvious bot shapes that organic traffic never produces** &mdash;
   even on a server that is normally busy.

Anything flagged as a `bot_spike` also has its **player count substituted out
of the stored data** while the burst lasts (see "Data substitution" below),
so the inflated readings never contaminate snapshots, totals or graphs. The
raw master-reported value is preserved in `snapshots.players_raw` for review.

#### What gets flagged

| Shape | What the bot sees | Why organic traffic never does this |
|-------|-------------------|--------------------------------------|
| **Burst**       | A near-empty server (baseline &le; `BOT_BASELINE_MAX`, default 8) jumps to `BOT_SPIKE_MIN`+ players (default 40) over the last `BOT_SPIKE_POLLS` polls (default 2) | Real fills ramp up &mdash; people show up over minutes |
| **Instant max-out** | The previous poll was &le; `BOT_INSTANT_MAX_BASELINE` (default 1) and this poll is already `BOT_SPIKE_MIN`+ | A single-poll step from nothing to a packed room is impossible organically |
| **Plateau**     | A near-empty server (baseline &le; `BOT_BASELINE_MAX`) holding the exact same non-trivial count (&ge; `BOT_PLATEAU_MIN`, default 20) across `BOT_PLATEAU_POLLS` *distinct* master updates (default 8). Cached re-reads (the master often republishes the same `hbcounter` for several polls in a row) do not count. | A fresh fill that arrives and then sits perfectly idle across that many genuine refreshes, on a server that was empty just before, is the bot-fleet signature |
| **Implausible** | A burst that crosses `BOT_IMPLAUSIBLE_MIN` (default 200) | Numbers like this do not appear organically on any AO server |
| **Copycat**     | A burst whose exact player count is mirrored by `BOT_COPYCAT_MIN_PEERS`+ other servers on the same master *this poll* (default 2 peers, count &ge; 10) | Independent servers do not coincidentally land on the same player count |

When the burst subsides the bot logs a `bot_spike_end` so the data reflects
both edges of the event.

#### What does **not** get flagged (lenience rules)

- **Organic ramps.** If the previous poll was already at `BOT_RAMP_RATIO`
  (default 0.5) of the current count or higher, the bot treats it as
  gradual growth and skips the burst alert. Step shapes (instant max-out,
  implausible, copycat) ignore this rule &mdash; those are never organic.
- **Popular servers.** A server with an all-time peak at or above
  `BOT_POPULAR_PEAK_MIN` (default 25) is "known-busy". Its burst threshold is
  scaled to `peak * BOT_POPULAR_BURST_FACTOR` (default &times; 2), so a
  regular show that fills the room every weekend never re-flags. Obvious
  tells (plateau, instant max-out, implausible, copycat) still fire &mdash;
  popularity does not excuse a 0&rarr;300 jump or 8 identical readings in a
  row.
- **Busy-network nights.** Every poll the bot also looks at every *other*
  server on the same master. When the network's median peer count is at or
  above `BOT_BUSY_NETWORK_MEDIAN` (default 15), one server rising along with
  the rest is treated as part of an event, not a one-off fill. Step shapes
  and the obvious tells still fire.
- **Cached master snapshots.** The vanilla AO master only refreshes each
  server every few minutes, so consecutive bot polls often re-read the
  exact same record. The plateau detector ignores those duplicates (same
  `hbcounter` as the previous poll) so the master's publish cadence cannot
  manufacture a flat run on its own.
- **Sustainably busy servers.** A server whose baseline is already above
  `BOT_BASELINE_MAX` is not eligible for the plateau alert -- a player
  count holding steady on a server that has been steady for a while is
  organic activity (RP sessions stay idle in the room for stretches),
  not a fresh bot fill.

#### Data substitution

The bot detector does not just alert &mdash; it **keeps the bot readings
out of the stored data**. The moment a server enters a spike, the bot
captures its pre-spike baseline (the median of its recent polls) into
`servers.bot_baseline`. While the spike lasts:

- `snapshots.players` is written as that captured baseline, so graphs and
  per-server history stay at the server's real player count
- `polls.player_count` and `poll_sources.player_count` sum the substituted
  values, so global and per-master player-count trends never carry the
  inflated burst
- `snapshots.players_raw` keeps the raw master-reported number for forensic
  review, so the unfiltered series is recoverable without polluting the
  graphs

When the count falls back below `BOT_SPIKE_MIN` the spike state clears and
substitution stops. The "Botted" tab on the dashboard surfaces every
attempt per master &mdash; how many bot fills each master has seen, how
many distinct servers were affected, how many are currently spiking and a
list of the latest offenders.

#### Tunable thresholds

Every detector knob is read from the environment so the bot can be retuned
without a code change. Leave a variable blank (or unset) to keep its
default. The full list:

| Variable | Default | What it controls |
|----------|---------|------------------|
| `BOT_SPIKE_MIN` | `40` | Players needed for a burst to qualify |
| `BOT_SPIKE_MAX` | `100` | Top of the typical bot-fill band (above this the alert message notes it cleared the band) |
| `BOT_BASELINE_MAX` | `8` | A server whose recent median is at or below this is "normally empty" and eligible for burst detection |
| `BOT_SPIKE_POLLS` | `2` | How many polls the burst must appear within |
| `BOT_BASELINE_WINDOW` | `30` | How many recent snapshots define the baseline (~30 min at the default 1-min poll) |
| `BOT_PLATEAU_POLLS` | `8` | Identical-count run length for the plateau detector |
| `BOT_PLATEAU_MIN` | `20` | Plateau ignores trivial counts below this |
| `BOT_INSTANT_MAX_BASELINE` | `1` | A prev-poll count at or below this turns a burst into an instant max-out |
| `BOT_IMPLAUSIBLE_MIN` | `200` | Bursts at or above this are flagged regardless of any lenience |
| `BOT_RAMP_RATIO` | `0.5` | Skip burst alert when the prior poll was already this fraction of the current count (gradual organic growth) |
| `BOT_POPULAR_PEAK_MIN` | `25` | All-time peak at or above this marks a server as "popular" |
| `BOT_POPULAR_BURST_FACTOR` | `2.0` | Popular server's burst threshold = `peak * factor` |
| `BOT_BUSY_NETWORK_MEDIAN` | `15` | Median peer player count at or above this triggers busy-network lenience |
| `BOT_COPYCAT_MIN_PEERS` | `2` | Number of other servers showing the same exact count to flag a copycat |
| `BOT_COPYCAT_MIN_COUNT` | `10` | Copycat detection ignores counts below this |

The same list is in `.env.example` with inline notes. Step-shaped tells
(instant, implausible, copycat) ignore the ramp / popularity /
busy-network lenience by design &mdash; those shapes are never organic,
so making them strict-bypass keeps the detector useful even on a server
or a network configured for very high tolerance. The plateau detector
follows the same near-empty-baseline rule as burst and is suppressed on
busy-network nights, because a flat count on a server that has been
sustainably busy is just steady RP activity.

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
| Pink line (player axis) | A suspected bot burst on the player count |

A summary box on the chart counts the rollovers, restarts, drops,
offline/return cycles and suspected bot bursts over the graphed span.

`/compare` is the "Ultimate statistician" view: it compares **every server
ever tracked** against one another in a single chart &mdash; the global
Attorney Online player count, per-server player counts over time, server
uptime, and peak/mean player ranking. **Every server is sorted into a
reliability tier** (rock solid / stable / flaky / rarely online) and listed in
the embed, so no server is left unranked; if there are too many servers to fit
in the embed, the full roster is attached as `server_tiers.txt`. The chart
downsamples its history into time buckets, so it stays readable whether the
window is an hour or several years. It accepts the same time filters as
`/playercount`.

`/hblist` is the dedicated heartbeat statistician: it documents and graphs the
HB counter of **every tracked server**, with all of its logged heartbeat
events marked on the timeline. **Suspicious events &mdash; an unexplained
drop, a manual reset, or an impossible jump &mdash; are the clear signs of
counter tampering, and are always drawn as bold red lines.** A server showing
any of them gets a red-tinted panel and a `POSSIBLE TAMPERING` verdict, so a
messed-with counter is impossible to miss; benign events (normal rollovers,
genuine restarts, offline/return cycles) are drawn faint. It has two views:

- **Paged** &mdash; one chart per server, six per page, scrolled with
  **Prev / Next buttons**
- **Global** &mdash; every server's HB counter on one combined graph, with a
  tampering summary listing every server whose counter was messed with

## Commands

| Command | Description |
|---------|-------------|
| `/list` | Show the current server list |
| `/server <query>` | Details and recent history for one server; `query` is part of a server name or an exact `ip:port` address |
| `/graph <query>` | Historical HB-counter and players graph for a server; `query` is part of a server name or an exact `ip:port` address |
| `/playercount [period] [view]` | Global player-count graph: trend or daily peak/low, filterable by day/week/month/year/all time |
| `/compare [period]` | Ultimate statistician: compare every server's player counts, uptime and peak/mean stats together |
| `/hblist [view] [period]` | HB-counter overview for every server with tampering flagged in bold red; `view` is paged (Prev/Next buttons) or global (all servers on one graph) |
| `/anomalies [count] [alerts_only]` | Show recently detected anomalies |
| `/deadservers` | List servers absent from the master list for `DEAD_SERVER_DAYS`+ days (default 60), treated as shut down; history is kept and a server that returns drops off the list |
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

## The web dashboard (optional)

The bot can also run a live web dashboard &mdash; the same data the Discord
commands show, but in a browser, with no Discord needed to look at it. It runs
inside the same process and reads the same database, so there is nothing
extra to start.

Turn it on in your `.env`:

```bash
WEBSITE_ENABLED=1
WEBSITE_HOST=0.0.0.0     # 0.0.0.0 = reachable from other machines; 127.0.0.1 = local only
WEBSITE_PORT=8080
WEBSITE_TITLE=My AO Dashboard
```

Restart the bot, then open `http://YOUR-SERVER-IP:8080` in any browser. It is
a full dashboard with a tab for everything the bot tracks:

- **Dashboard** &mdash; headline stats, the global player-count chart and a
  live anomaly feed
- **Servers** &mdash; every server ever tracked in one searchable, sortable
  table (players, peak, mean, uptime %, snapshots, anomalies&hellip;), plus a
  top-15 peak-players ranking. When more than one master is configured, a
  **Master** toggle here (and on the Dashboard) switches which master's
  server list you are viewing
- **Players** &mdash; the global player count as a continuous trend *and* a
  per-day peak/average/low breakdown, for both players and servers online
- **Activity** &mdash; a busiest-times heatmap showing average players by
  hour of day and day of week
- **Compare** &mdash; hand-pick any servers (or use the auto busiest dozen)
  to overlay on one graph, reliability tiers, and an uptime ranking
- **HB Counter** &mdash; every server's heartbeat counter on one graph with
  suspected tampering flagged red, plus a per-server status table
- **Anomalies** &mdash; a filterable browser (by type, severity and time
  range) with an anomalies-by-type chart
- **Records** &mdash; all-time milestones: highest player count, busiest day,
  biggest server, most data collected and more
- **Dead Servers** &mdash; servers absent long enough to be considered shut down
- **Botted** &mdash; per-master scoreboard of suspected bot-fill attempts
  with the currently-spiking count and the latest offenders (see
  [Bot-pattern detection](#bot-pattern-detection))
- **Master Server** &mdash; about our open-source master server and how to
  list your own AO server on it, with links to the master server and WebAO
  source code

Every chart has hover tooltips, and **every tab can be filtered by day,
week, month, year, all-time, or a specific UTC calendar day**. The named
ranges (Day / Week / Month / Year / All time) are the pill buttons at the
top of each tab; next to them is a date picker &mdash; pick any date and
every chart, table, count, anomaly browser and export on the page rescopes
to *just that UTC day*. Hit **Clear** next to the date picker to drop back
to all-time. The selection sticks as you switch between tabs.

Clicking any server anywhere opens a detail view with the server's
**published room description** (when it publishes one on the master list),
the WebAO join link, an uptime timeline (online/offline strip with
outages), player history, HB-counter history, daily breakdown and
anomalies. The description is also visible in the dashboard and Servers
list as a clipped sub-line under each name (full text on hover). Most tabs
have CSV/JSON export buttons, and server and compare views have shareable
links. The dashboard is strictly read-only &mdash; it never polls or posts
anything.

It refreshes itself every 60 seconds. If you expose it to the internet, put it
behind a reverse proxy (nginx, Caddy) for HTTPS, or keep `WEBSITE_HOST` on
`127.0.0.1` and tunnel in over SSH.

## Polling multiple master servers

`MS_URL` accepts **several endpoints at once** &mdash; list them separated by
commas (or one per line) and the bot polls them all. **By default it polls
two masters:** the official Attorney Online list and our open-source master
server (`servers.umineko.online`, see
[Setting up your own master server](#setting-up-your-own-master-server)).

```bash
# The default -- the official master plus our open-source one:
MS_URL=https://servers.aceattorneyonline.com/servers,https://servers.umineko.online/servers

# Just the official master:
MS_URL=https://servers.aceattorneyonline.com/servers
```

How it behaves:

- **Every master is polled together**, once per `POLL_INTERVAL_MINUTES`. The
  requests run concurrently, so adding masters does not slow polling down.
- **The lists are merged** into one combined view. A server is identified by
  its `ip:port`, so if the same server appears on two masters it is counted
  once; the first master in the list "wins" as its recorded source.
- **A failing master never blacks out the others.** If one master is
  unreachable it is skipped and logged, and the poll still succeeds on
  whatever the reachable masters returned. A poll only counts as failed when
  *every* master fails.
- **Each server remembers which master listed it.** This is stored so the
  dashboard can filter by master (see below).
- **Each master's poll stats are kept separate.** Every poll records its
  player and server counts per master, so the two never blur together &mdash;
  the Players page draws one trend line per master alongside the combined
  total.

### Switching masters on the dashboard

When two or more masters are configured, the web dashboard shows a **Master**
toggle on the **Dashboard** and **Servers** pages. It is a row of buttons:
`All` plus one button per master (labelled by its host name). Click a button
to swap the server list to just that master's servers; `All` shows the merged
list. The toggle is hidden when only one master is configured, so a normal
single-master setup is unaffected.

The toggle filters the server-list views. The **Players** page additionally
breaks its player-count and servers-online trends down per master &mdash; one
line per master server next to the combined total &mdash; so each master's
stats stay separate. Other aggregate pages (Compare, HB Counter, Activity,
Records) still show combined data across every master, since those charts
span the whole tracked history.

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

## WebAO join links

Every server the bot tracks that publishes a websocket port can be joined
straight from a browser. The bot reads the `ws_port` and `wss_port` fields
from the master server list and builds a [WebAO](https://github.com/AttorneyOnline/webAO)
client link:

- a server with a **secure** websocket (`wss_port`) gets a
  `https://&hellip;/client.html?&hellip;connect=wss://&hellip;` link
- a server with only a plain websocket (`ws_port`) gets the matching
  `http://&hellip;/client.html?&hellip;connect=ws://&hellip;` link
- a server that publishes neither has no WebAO link (it cannot be joined in a
  browser)

The link appears in `/server`, on the dashboard's server tables (a **&#9654;
Join** button per server) and in each server's detail view. The WebAO client
host defaults to `webao.miku.pizza` and is configurable with `WEBAO_CLIENT`.

## Using your own master list and WebAO

Nothing in this bot is hard-wired to the official Attorney Online
infrastructure. It talks to two endpoints, and both are plain config values:

- `MS_URL` &mdash; the master server list it polls (default
  `https://servers.aceattorneyonline.com/servers`). List several URLs separated
  by commas to poll **more than one master at once** &mdash; their server lists
  are merged and deduplicated by `ip:port`, and an unreachable master is skipped
  without blacking out the others. When two or more masters are configured, the
  web dashboard shows a **Master** toggle on the Dashboard and Servers pages so
  you can switch which master's server list you are looking at.
- `WEBAO_CLIENT` &mdash; the WebAO client host used to build join links
  (default `webao.miku.pizza/client.html`)

Point those two at your own infrastructure and the whole bot &mdash; polling,
history, anomaly detection, graphs and the dashboard &mdash; works against it
unchanged.

### What the master list has to return

`MS_URL` just needs to answer an HTTP `GET` with a JSON **array of server
objects**. The bot reads these fields per object (see `monitor.py`):

| Field | Required | Notes |
|-------|----------|-------|
| `ip` | yes | Host or IP. May be markdown-wrapped (`[host](url)`) &mdash; the bot unwraps it |
| `port` | yes | Game port, must parse as an integer |
| `name` | no | Display name; defaults to `(unnamed)` |
| `description` | no | Free text |
| `players` | no | Current player count; defaults to `0` |
| `hbcounter` | no | Ever-rising heartbeat counter; omit it and HB anomaly detection is simply skipped for that server |
| `ws_port` | no | Plain websocket port &mdash; enables an `http://` WebAO join link |
| `wss_port` | no | Secure websocket port &mdash; enables an `https://` WebAO join link |

A minimal valid response:

```json
[
  {
    "ip": "203.0.113.10",
    "port": 27016,
    "name": "My Server",
    "description": "A test server",
    "players": 4,
    "hbcounter": 1234,
    "ws_port": 50001,
    "wss_port": 50002
  }
]
```

That is exactly the shape the official `tsuserver`/master server already
emits, so any standard AO master server works out of the box. If you write
your own list service, any web framework that returns that JSON array is
enough &mdash; the bot only ever does a `GET` and never writes back.

### Realistic ways to run your own

1. **Run our open-source master server (recommended).** We maintain a
   Python master server &mdash;
   **[Nyan-AO-Master-Server](https://github.com/SyntaxNyah/Nyan-AO-Master-Server)**
   &mdash; that you can host yourself. It accepts heartbeats from your AO
   servers and publishes exactly the JSON array this bot expects. Follow the
   setup instructions in that repository, then point the bot at it with
   `MS_URL=https://your-host/servers`. See
   [Setting up your own master server](#setting-up-your-own-master-server)
   below.

2. **Run the official master server.** The official Attorney Online master
   server is not open source
   ([AttorneyOnline/master](https://github.com/AttorneyOnline/master)). If you
   have access to it you can host it yourself and set `MS_URL` the same way.

3. **Serve a static or generated JSON file.** If you only run a handful of
   servers and do not need real heartbeating, you can publish the array above
   as a static file (or generate it from your own script/cron) and point
   `MS_URL` at it. The bot does not care how the JSON is produced, only that
   it is current when polled &mdash; `POLL_INTERVAL_MINUTES` controls how often.

4. **Proxy or filter the official list.** Point `MS_URL` at a small service
   of your own that fetches the official list and trims it to just your
   community's servers (or merges in extra ones). The bot then tracks exactly
   that curated set.

### Setting up your own master server

If you want a real, open-source master server rather than a static file or a
proxy, use **[Nyan-AO-Master-Server](https://github.com/SyntaxNyah/Nyan-AO-Master-Server)**
&mdash; our self-hostable Python master server. It accepts heartbeats from
your Attorney Online servers and serves the JSON server list this bot polls,
in exactly the shape described above.

Full installation and configuration instructions live in the
[Nyan-AO-Master-Server repository](https://github.com/SyntaxNyah/Nyan-AO-Master-Server).
Once it is running, point the bot at it:

```bash
MS_URL=https://your-master-host/servers
```

Everything else &mdash; polling, history, anomaly detection, graphs and the
dashboard &mdash; works against it unchanged.

### Running your own WebAO

WebAO ([AttorneyOnline/webAO](https://github.com/AttorneyOnline/webAO)) is a
static browser build &mdash; host it anywhere (GitHub Pages, nginx, a CDN) and
set `WEBAO_CLIENT` to its `client.html` path, with no scheme:

```bash
WEBAO_CLIENT=ao.example.com/webao/client.html
```

The bot picks `http://` or `https://` automatically to match each server's
`ws_port` / `wss_port`, and appends the `connect=` query the client expects.
For browser join links to work end to end you need three things lined up:
your WebAO build is reachable over HTTPS, your AO servers expose a websocket
port, and that port is published as `ws_port`/`wss_port` in your master list.
A server that publishes neither websocket port simply gets no join link.

#### Recommended: the LemmyAO fork

On our own site we do not run stock upstream WebAO &mdash; we run
**[LemmyAO](https://github.com/SyntaxNyah/LemmyAO)**, our maintained fork of
WebAO. It is the build behind the join links you see on our dashboard, and it
is what we recommend you host if you want the same experience.

To set up your own WebAO with LemmyAO:

1. **Clone the fork** and build it &mdash; follow the instructions in the
   [LemmyAO repository](https://github.com/SyntaxNyah/LemmyAO). Like upstream
   WebAO it produces a static browser build, so no special server runtime is
   needed.
2. **Host the build** anywhere that can serve static files over HTTPS
   &mdash; GitHub Pages, nginx, Caddy, or a CDN all work. HTTPS matters: a
   secure (`wss://`) connection will be blocked if the page itself is served
   over plain `http://`.
3. **Point the bot at it** by setting `WEBAO_CLIENT` to your build's
   `client.html` path, with no scheme:

   ```bash
   WEBAO_CLIENT=ao.example.com/lemmyao/client.html
   ```

That is all the bot needs &mdash; it builds every join link against that host
automatically. If you would rather not self-host, you can also point
`WEBAO_CLIENT` straight at our hosted LemmyAO build, or leave it on the
default (`webao.miku.pizza/client.html`).

### Putting it together

A fully self-hosted setup is just these `.env` values pointed at your own
hosts:

```bash
MS_URL=https://ms.example.com/servers
WEBAO_CLIENT=ao.example.com/webao/client.html
POLL_INTERVAL_MINUTES=1
WEBSITE_ENABLED=1
WEBSITE_TITLE=My Community Server Tracker
```

Everything else &mdash; the SQLite history, anomaly detection, graphs, the
dashboard and all slash commands &mdash; behaves identically; it is all
driven by whatever the configured `MS_URL` returns.

## Credits

- **AO-MS-BOT** &mdash; created and maintained by
  [@SyntaxNyah](https://github.com/SyntaxNyah).
- **WebAO** &mdash; the browser client for Attorney Online. On our site the
  "join in browser" links use [LemmyAO](https://github.com/SyntaxNyah/LemmyAO),
  our maintained fork of WebAO; the bot's default `WEBAO_CLIENT` falls back to
  the fork hosted at [webao.miku.pizza](https://webao.miku.pizza/). Both are
  based on the upstream
  [AttorneyOnline/webAO](https://github.com/AttorneyOnline/webAO) project. All
  credit for the web client goes to its authors and those forks' maintainers.
