"""Optional live web dashboard.

Runs inside the bot's own event loop and serves a single-page dashboard plus
a small JSON API, all reading from the same SQLite database the bot writes.
Enable it with WEBSITE_ENABLED=1 -- everything else has a sensible default.
"""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

from aiohttp import web

import config
import database as db

log = logging.getLogger("website")

_PERIODS = {
    "day": timedelta(days=1),
    "week": timedelta(weeks=1),
    "month": timedelta(days=30),
    "year": timedelta(days=365),
}


def _since(period):
    """Turn a period name into an ISO lower bound, or None for all history."""
    delta = _PERIODS.get(period)
    if delta is None:
        return None
    return (datetime.now(timezone.utc) - delta).isoformat()


def _downsample(rows, limit=500):
    """Thin a long row list down to at most `limit` evenly-spaced points."""
    if len(rows) <= limit:
        return list(rows)
    step = len(rows) / limit
    return [rows[int(i * step)] for i in range(limit)]


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------

async def api_overview(request):
    """Everything the front page needs in a single call."""
    period = request.query.get("period", "all")
    since = _since(period)

    stats = await asyncio.to_thread(db.stats)
    snaps = await asyncio.to_thread(db.last_poll_servers)
    servers = await asyncio.to_thread(db.all_servers)
    anomalies = await asyncio.to_thread(db.recent_anomalies, 50, False)
    polls = await asyncio.to_thread(db.poll_history, since)

    status = {s["server_key"]: s["status"] for s in servers}
    server_list = sorted(
        ({"key": s["server_key"], "name": s["name"],
          "players": s["players"],
          "hb": s["hbcounter"],
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


async def api_server(request):
    """Detail, history and anomalies for one server."""
    key = request.query.get("key", "")
    period = request.query.get("period", "all")
    since = _since(period)

    srv = await asyncio.to_thread(db.get_server, key)
    if srv is None:
        return web.json_response({"error": "Unknown server."}, status=404)

    snap = await asyncio.to_thread(db.latest_snapshot, key)
    hist = await asyncio.to_thread(db.server_history, key, None, since)
    anoms = await asyncio.to_thread(db.server_anomalies, key, 60)

    return web.json_response({
        "server": dict(srv),
        "snapshot": dict(snap) if snap else None,
        "history": [{"t": r["ts"], "p": r["players"], "hb": r["hbcounter"]}
                    for r in _downsample(hist)],
        "anomalies": [dict(a) for a in anoms],
    })


async def index(request):
    return web.Response(text=PAGE, content_type="text/html")


# --------------------------------------------------------------------------
# Server lifecycle
# --------------------------------------------------------------------------

async def start():
    """Start the dashboard inside the running event loop. Returns the runner."""
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/api/overview", api_overview)
    app.router.add_get("/api/server", api_server)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBSITE_HOST, config.WEBSITE_PORT)
    await site.start()
    log.info("web dashboard live at http://%s:%s",
             config.WEBSITE_HOST, config.WEBSITE_PORT)
    return runner


# --------------------------------------------------------------------------
# The single-page dashboard (HTML + CSS + JS, no external dependencies)
# --------------------------------------------------------------------------

PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b25; --panel2:#1c2230; --line:#2a3140;
    --txt:#e6edf3; --dim:#8b95a5; --accent:#7c9cff; --green:#41c97a;
    --red:#f0524b; --orange:#e8a13a; --blue:#4f9dff; --purple:#a472f0;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body {
    background:var(--bg); color:var(--txt); font-family:"Segoe UI",
    system-ui, sans-serif; line-height:1.5; padding-bottom:60px;
  }
  a { color:var(--accent); }
  header {
    background:linear-gradient(120deg,#1a2740,#3a2a55 60%,#1f3a3a);
    padding:26px 22px; border-bottom:1px solid var(--line);
  }
  header h1 { font-size:24px; letter-spacing:.3px; }
  header p { color:var(--dim); font-size:13px; margin-top:4px; }
  .wrap { max-width:1180px; margin:0 auto; padding:0 18px; }
  .stats {
    display:grid; gap:14px; margin:22px 0;
    grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
  }
  .stat {
    background:var(--panel); border:1px solid var(--line);
    border-radius:12px; padding:14px 16px;
  }
  .stat .v { font-size:26px; font-weight:700; }
  .stat .l { color:var(--dim); font-size:12px; text-transform:uppercase;
             letter-spacing:.6px; margin-top:2px; }
  .card {
    background:var(--panel); border:1px solid var(--line);
    border-radius:14px; padding:18px; margin-bottom:20px;
  }
  .card h2 { font-size:16px; margin-bottom:12px; display:flex;
             justify-content:space-between; align-items:center; }
  .periods { display:flex; gap:6px; flex-wrap:wrap; }
  .periods button {
    background:var(--panel2); color:var(--dim); border:1px solid var(--line);
    border-radius:20px; padding:4px 13px; font-size:12px; cursor:pointer;
  }
  .periods button.on { background:var(--accent); color:#0d1117;
                        border-color:var(--accent); font-weight:600; }
  .grid2 { display:grid; gap:20px; grid-template-columns:1.4fr 1fr; }
  @media (max-width:820px){ .grid2{ grid-template-columns:1fr; } }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  th { text-align:left; color:var(--dim); font-weight:600; font-size:11px;
       text-transform:uppercase; padding:6px 8px; border-bottom:1px solid var(--line); }
  td { padding:7px 8px; border-bottom:1px solid #20262f; }
  tr.srv { cursor:pointer; }
  tr.srv:hover td { background:var(--panel2); }
  .pill { font-size:11px; padding:2px 8px; border-radius:20px; font-weight:600; }
  .on-pill { background:rgba(65,201,122,.16); color:var(--green); }
  .off-pill { background:rgba(240,82,75,.16); color:var(--red); }
  .num { font-variant-numeric:tabular-nums; font-weight:600; }
  .chart { width:100%; height:260px; display:block; }
  .empty { color:var(--dim); padding:30px; text-align:center; font-size:13px; }
  .anom { padding:9px 4px; border-bottom:1px solid #20262f; font-size:13px; }
  .anom:last-child { border:0; }
  .anom .top { display:flex; gap:8px; align-items:center; }
  .anom .dot { width:9px; height:9px; border-radius:50%; flex:none; }
  .anom .ty { font-weight:600; }
  .anom .ts { color:var(--dim); font-size:11px; margin-left:auto; }
  .anom .detail { color:var(--dim); font-size:12px; margin:2px 0 0 17px; }
  .anom .who { color:var(--txt); font-size:12px; margin-left:17px; }
  footer { color:var(--dim); font-size:12px; text-align:center; margin-top:10px; }
  .modal {
    position:fixed; inset:0; background:rgba(0,0,0,.6); display:none;
    align-items:flex-start; justify-content:center; padding:40px 16px;
    overflow:auto; z-index:50;
  }
  .modal.show { display:flex; }
  .sheet {
    background:var(--panel); border:1px solid var(--line); border-radius:16px;
    max-width:900px; width:100%; padding:22px;
  }
  .sheet h2 { font-size:19px; }
  .close { float:right; background:var(--panel2); border:1px solid var(--line);
           color:var(--txt); border-radius:8px; padding:5px 12px; cursor:pointer; }
  .kv { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(120px,1fr));
        margin:14px 0; }
  .kv div { background:var(--panel2); border:1px solid var(--line);
            border-radius:10px; padding:9px 12px; }
  .kv .l { color:var(--dim); font-size:11px; text-transform:uppercase; }
  .kv .v { font-size:16px; font-weight:600; }
  .charth { color:var(--dim); font-size:12px; margin:14px 0 4px; }
  .live { display:inline-block; width:8px; height:8px; border-radius:50%;
          background:var(--green); margin-right:6px;
          animation:pulse 2s infinite; }
  @keyframes pulse { 50%{ opacity:.3; } }
</style>
</head>
<body>
<header><div class="wrap">
  <h1>__TITLE__</h1>
  <p><span class="live"></span>Live Attorney Online master-server dashboard
     &mdash; <span id="updated">loading&hellip;</span></p>
</div></header>

<div class="wrap">
  <div class="stats" id="stats"></div>

  <div class="card">
    <h2>Global player count
      <span class="periods" id="periods"></span>
    </h2>
    <div id="playerchart"><div class="empty">Loading&hellip;</div></div>
  </div>

  <div class="grid2">
    <div class="card">
      <h2>Servers <span class="num" id="srvcount" style="color:var(--dim)"></span></h2>
      <div id="servers"><div class="empty">Loading&hellip;</div></div>
    </div>
    <div class="card">
      <h2>Recent anomalies</h2>
      <div id="anomalies"><div class="empty">Loading&hellip;</div></div>
    </div>
  </div>

  <footer>Auto-refreshes every 60 seconds &middot; click any server for its
    full history.</footer>
</div>

<div class="modal" id="modal"><div class="sheet" id="sheet"></div></div>

<script>
const PERIODS = ["day","week","month","year","all"];
const PLABEL = {day:"Day",week:"Week",month:"Month",year:"Year",all:"All time"};
let period = "all";

const SEV = {alert:"var(--red)", low:"var(--orange)", info:"var(--blue)",
             "high":"var(--red)"};

function el(tag, cls, txt){
  const e = document.createElement(tag);
  if(cls) e.className = cls;
  if(txt !== undefined) e.textContent = txt;
  return e;
}
function fmtTs(s){ return s ? s.slice(0,16).replace("T"," ") : "-"; }

// Draw a smooth area chart into an SVG string. pts: [{t, ...}], key picks value.
function chart(pts, key, color){
  if(!pts || pts.length < 2)
    return '<div class="empty">Not enough data yet for this range.</div>';
  const vals = pts.map(p=>p[key]).filter(v=>v!==null && v!==undefined);
  if(vals.length < 2)
    return '<div class="empty">Not enough data yet for this range.</div>';
  const W=1000, H=260, pad=16;
  let max=Math.max(...vals), min=Math.min(...vals);
  if(max===min) max=min+1;
  const span=max-min;
  const x=i=>pad + i*(W-2*pad)/(pts.length-1);
  const y=v=>H-pad - (v-min)/span*(H-2*pad);
  let line="", first=true;
  pts.forEach((p,i)=>{
    const v=p[key]; if(v===null||v===undefined) return;
    line += (first?"M":"L") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " ";
    first=false;
  });
  const area = line + "L"+x(pts.length-1).toFixed(1)+" "+(H-pad)+
               " L"+x(0).toFixed(1)+" "+(H-pad)+" Z";
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <path d="${area}" fill="${color}" fill-opacity="0.14"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2.5"
      stroke-linejoin="round" stroke-linecap="round"/>
    <text x="${pad}" y="14" fill="var(--dim)" font-size="13">peak ${max}</text>
    <text x="${pad}" y="${H-4}" fill="var(--dim)" font-size="13">low ${min}</text>
  </svg>`;
}

function renderStats(s){
  const box = document.getElementById("stats");
  box.innerHTML = "";
  const items = [
    ["Servers online", s.online, "var(--green)"],
    ["Servers known", s.known, "var(--txt)"],
    ["Successful polls", s.polls, "var(--blue)"],
    ["Snapshots stored", s.snapshots, "var(--purple)"],
    ["Anomalies", s.anomalies, "var(--orange)"],
    ["Alerts", s.alerts, "var(--red)"],
    ["Bot bursts", s.bot_spikes, "var(--orange)"],
  ];
  for(const [l,v,c] of items){
    const d = el("div","stat");
    const vv = el("div","v", (v??0).toLocaleString()); vv.style.color=c;
    d.appendChild(vv);
    d.appendChild(el("div","l", l));
    box.appendChild(d);
  }
}

function renderServers(list){
  document.getElementById("srvcount").textContent =
    list.length ? "(" + list.length + ")" : "";
  const box = document.getElementById("servers");
  if(!list.length){ box.innerHTML='<div class="empty">No servers yet.</div>'; return; }
  const t = el("table");
  t.innerHTML = "<thead><tr><th>Server</th><th>Players</th>"+
                "<th>HB</th><th>Status</th></tr></thead>";
  const tb = el("tbody");
  for(const s of list){
    const tr = el("tr","srv");
    tr.onclick = ()=>openServer(s.key);
    const nm = el("td"); nm.appendChild(el("strong", null, s.name));
    tr.appendChild(nm);
    tr.appendChild(el("td","num", String(s.players)));
    tr.appendChild(el("td","num", s.hb==null?"-":String(s.hb)));
    const st = el("td");
    const pill = el("span", "pill " + (s.status==="online"?"on-pill":"off-pill"),
                    s.status);
    st.appendChild(pill); tr.appendChild(st);
    tb.appendChild(tr);
  }
  t.appendChild(tb);
  box.innerHTML = ""; box.appendChild(t);
}

function renderAnomalies(list){
  const box = document.getElementById("anomalies");
  if(!list.length){
    box.innerHTML='<div class="empty">No anomalies recorded. All clear!</div>';
    return;
  }
  box.innerHTML = "";
  for(const a of list){
    const d = el("div","anom");
    const top = el("div","top");
    const dot = el("span","dot");
    dot.style.background = SEV[a.severity] || "var(--dim)";
    top.appendChild(dot);
    top.appendChild(el("span","ty", a.type));
    top.appendChild(el("span","ts", fmtTs(a.ts)));
    d.appendChild(top);
    d.appendChild(el("div","who", a.name || ""));
    d.appendChild(el("div","detail", a.detail || ""));
    box.appendChild(d);
  }
}

function renderPeriods(){
  const box = document.getElementById("periods");
  box.innerHTML = "";
  for(const p of PERIODS){
    const b = el("button", p===period?"on":"", PLABEL[p]);
    b.onclick = ()=>{ period=p; load(); };
    box.appendChild(b);
  }
}

async function load(){
  renderPeriods();
  try {
    const r = await fetch("/api/overview?period=" + period);
    const d = await r.json();
    renderStats(d.stats);
    renderServers(d.servers);
    renderAnomalies(d.anomalies);
    document.getElementById("playerchart").innerHTML =
      chart(d.players, "p", "var(--green)");
    document.getElementById("updated").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch(e){
    document.getElementById("updated").textContent = "connection lost";
  }
}

async function openServer(key){
  const modal = document.getElementById("modal");
  const sheet = document.getElementById("sheet");
  sheet.innerHTML = '<div class="empty">Loading&hellip;</div>';
  modal.classList.add("show");
  try {
    const r = await fetch("/api/server?period="+period+
                          "&key="+encodeURIComponent(key));
    const d = await r.json();
    if(d.error){ sheet.innerHTML='<div class="empty">'+d.error+'</div>'; return; }
    const s = d.server, snap = d.snapshot;
    sheet.innerHTML = "";
    const close = el("button","close","Close");
    close.onclick = ()=>modal.classList.remove("show");
    sheet.appendChild(close);
    sheet.appendChild(el("h2", null, s.name));
    const addr = el("p", null, s.server_key);
    addr.style.color="var(--dim)"; addr.style.fontSize="12px";
    sheet.appendChild(addr);

    const kv = el("div","kv");
    const pairs = [
      ["Status", s.status],
      ["Players", snap ? snap.players : "-"],
      ["HB counter", snap && snap.hbcounter!=null ? snap.hbcounter : "-"],
      ["First seen", fmtTs(s.first_seen)],
      ["Last seen", fmtTs(s.last_seen)],
      ["Data points", d.history.length],
    ];
    for(const [l,v] of pairs){
      const c = el("div");
      c.appendChild(el("div","l", l));
      c.appendChild(el("div","v", String(v)));
      kv.appendChild(c);
    }
    sheet.appendChild(kv);

    sheet.appendChild(el("div","charth","Players over time ("+PLABEL[period]+")"));
    const pc = el("div");
    pc.innerHTML = chart(d.history, "p", "var(--blue)");
    sheet.appendChild(pc);

    sheet.appendChild(el("div","charth","HB counter over time"));
    const hc = el("div");
    hc.innerHTML = chart(d.history, "hb", "var(--purple)");
    sheet.appendChild(hc);

    sheet.appendChild(el("div","charth","Anomalies ("+d.anomalies.length+")"));
    if(!d.anomalies.length){
      sheet.appendChild(el("div","empty","No anomalies for this server."));
    } else {
      for(const a of d.anomalies){
        const an = el("div","anom");
        const top = el("div","top");
        const dot = el("span","dot");
        dot.style.background = SEV[a.severity] || "var(--dim)";
        top.appendChild(dot);
        top.appendChild(el("span","ty", a.type));
        top.appendChild(el("span","ts", fmtTs(a.ts)));
        an.appendChild(top);
        an.appendChild(el("div","detail", a.detail || ""));
        sheet.appendChild(an);
      }
    }
  } catch(e){
    sheet.innerHTML = '<div class="empty">Could not load server.</div>';
  }
}

document.getElementById("modal").onclick = (e)=>{
  if(e.target.id === "modal") e.target.classList.remove("show");
};
document.addEventListener("keydown", (e)=>{
  if(e.key === "Escape") document.getElementById("modal").classList.remove("show");
});

load();
setInterval(load, 60000);
</script>
</body>
</html>
"""

PAGE = PAGE.replace("__TITLE__", config.WEBSITE_TITLE)
