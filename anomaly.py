"""Heartbeat-counter and player-count analysis.

A server's hbcounter rises by roughly 1 per minute. When it reaches its cap it
rolls over and continues from (cap - rollover_drop); that is normal. Any change
the rate model and rollover cannot explain is flagged.

The cap, rollover and tolerances differ per master server, so analyze_hb takes
a `rules` profile (see config.ms_rules) -- the vanilla Attorney Online master
and the Umineko Online master each get their own rules.

The player count is watched separately: a near-empty server that suddenly
fills with players over a poll or two is flagged as a suspected bot pattern.
"""
import statistics

from config import (BOT_BASELINE_MAX, BOT_BASELINE_WINDOW,
                    BOT_BUSY_NETWORK_MEDIAN, BOT_COPYCAT_MIN_COUNT,
                    BOT_COPYCAT_MIN_PEERS, BOT_IMPLAUSIBLE_MIN,
                    BOT_INSTANT_MAX_BASELINE, BOT_PLATEAU_MIN,
                    BOT_PLATEAU_POLLS, BOT_POPULAR_BURST_FACTOR,
                    BOT_POPULAR_PEAK_MIN, BOT_RAMP_RATIO, BOT_SPIKE_MAX,
                    BOT_SPIKE_MIN, BOT_SPIKE_POLLS, VANILLA_HB_RULES)


def analyze_hb(prev_hb, cur_hb, elapsed_min, reliable=True, rules=None):
    """Compare a server's heartbeat counter between two polls.

    `rules` is the per-master rule profile (see config.ms_rules): it carries
    the cap/rollover values and tolerances for whichever master listed the
    server. When omitted the vanilla Attorney Online profile is used.

    Returns (type, severity, detail), or None when the change looks normal.
    `reliable` should be False when the gap between polls is unusually long or
    the server had been missing -- in that case findings are downgraded to info.
    """
    if prev_hb is None or cur_hb is None:
        return None

    rules = rules or VANILLA_HB_RULES
    hb_cap = rules["hb_cap"]
    rollover_drop = rules["rollover_drop"]
    rate_max = rules["hb_rate_max"]
    margin = rules["hb_margin"]
    jump_margin = rules["hb_jump_margin"]
    restart_window = rules["hb_restart_window"]
    real_restart_minutes = rules["hb_real_restart_minutes"]
    reset_edge_margin = rules["hb_reset_edge_margin"]
    label = rules["label"]

    elapsed_min = max(elapsed_min, 0.5)
    expected_max = elapsed_min * rate_max + margin
    delta = cur_hb - prev_hb

    if delta >= 0:
        # An upward jump is not always alarming. The vanilla master only
        # publishes the counter every few minutes, so when it refreshes the
        # counter leaps by all the minutes it accumulated meanwhile -- a
        # +20-30 step is routine there. The Umineko master is clock-anchored
        # and climbs smoothly, so its jump margin is far tighter. Either way
        # an upward jump is never escalated to a high-severity alert.
        jump_max = elapsed_min * rate_max + jump_margin
        if delta > jump_max:
            sev = "low" if reliable else "info"
            return ("hb_jump", sev,
                    f"HB jumped +{delta} in {elapsed_min:.1f} min "
                    f"(expected at most +{jump_max:.0f} on the {label} "
                    f"master).")
        return None

    # The counter decreased: a normal rollover at the cap, a fresh restart, or
    # a drop that nothing in the rate model can account for.
    real_gain = delta + rollover_drop            # value if one rollover happened
    could_roll = (prev_hb + expected_max) >= hb_cap
    if could_roll and (-margin <= real_gain <= expected_max):
        return ("hb_rollover", "info",
                f"HB rolled over {prev_hb} -> {cur_hb} "
                f"(normal {label} reset at {hb_cap}).")

    # A counter that has fallen to the floor (<= restart_window) usually means
    # the server was taken down and brought back. But a genuine restart takes
    # time: the master holds a dead entry for a while, so a real down-and-back
    # cycle leaves a long gap since the last reading. If the counter slammed to
    # the floor well short of real_restart_minutes elapsed, the server never
    # had time to actually go down -- that points to a hand-reset counter. A
    # small edge margin keeps a restart that came back just shy of the window
    # (poll timing jitter) from being mistaken for a manual reset.
    if 0 <= cur_hb <= restart_window:
        if elapsed_min < real_restart_minutes - reset_edge_margin:
            return ("hb_reset", "alert",
                    f"HB slammed {prev_hb} -> {cur_hb} after only "
                    f"{elapsed_min:.1f} min -- too fast for a genuine restart "
                    f"({real_restart_minutes} min on the {label} master), "
                    f"likely a manual reset.")
        return ("hb_restart", "info",
                f"HB reset {prev_hb} -> {cur_hb} -- server looks restarted "
                f"(counter within restart range after a {elapsed_min:.0f} min "
                f"gap).")

    sev = "alert" if reliable else "info"
    note = "" if reliable else " (long polling gap -- unverified)"
    return ("hb_drop", sev,
            f"HB DROPPED {prev_hb} -> {cur_hb} ({delta}) -- "
            f"not explained by a normal {label} rollover{note}.")


def analyze_players(recent_players, cur_players, prev_state="normal",
                    prev_baseline=None, server_peak=None, peer_counts=None,
                    recent_hbcounters=None, cur_hbcounter=None):
    """Spot a bot-like player burst on a normally near-empty server.

    `recent_players` is the server's player counts from prior polls,
    oldest-first and excluding the current poll. `cur_players` is this poll's
    count. `prev_state` is the server's stored bot-pattern state -- "normal"
    or "spike". `prev_baseline` is the baseline value captured at the moment
    the spike began (kept across polls so substitution stays stable).

    `server_peak` is the server's all-time peak (or peak over the recent
    history available); when it is at or above BOT_POPULAR_PEAK_MIN the
    server is treated as "popular" and its burst threshold is scaled up so
    a regular crowd never re-flags. `peer_counts` is this poll's player
    count on every *other* server listed on the same master -- it lets the
    detector see the wider network state and tell coordinated fills (many
    servers report the same count) apart from a busy night (many servers up
    organically).

    `recent_hbcounters` is the server's hbcounter readings from the same
    prior polls as `recent_players` (oldest-first), and `cur_hbcounter` is
    this poll's hbcounter. The plateau detector uses them to skip cached
    re-reads -- the vanilla Attorney Online master only publishes each
    server every few minutes, so the bot's per-minute polls re-read the
    same snapshot several times in a row. A "plateau" is only meaningful
    across *distinct* master updates, not across cache hits.

    Returns (new_state, new_baseline, filtered_players, anomaly).
      - `new_state`         next bot_state to persist
      - `new_baseline`      baseline to persist (None when not in a spike)
      - `filtered_players`  value safe to store in snapshots / poll totals;
                            equals cur_players normally, the pre-spike
                            baseline while a spike is in progress
      - `anomaly`           (type, severity, detail) or None

    Detection covers three obvious bot-fill shapes:
      * burst   -- jump from a near-empty baseline to the effective spike
                   minimum (scaled up on popular servers, suppressed when
                   the whole network is busy)
      * instant -- single-poll jump from baseline <= BOT_INSTANT_MAX_BASELINE
                   straight to BOT_SPIKE_MIN+ (no organic ramp)
      * plateau -- a normally-empty server (baseline <= BOT_BASELINE_MAX)
                   holding the exact same non-trivial count across
                   BOT_PLATEAU_POLLS *distinct* master updates in a row.
                   Cached re-reads (same hbcounter as the previous poll) are
                   ignored so the 5-minute publish cadence on the vanilla
                   AO master cannot manufacture a flat run on its own.
    A burst above BOT_IMPLAUSIBLE_MIN, or one whose exact count is mirrored
    on BOT_COPYCAT_MIN_PEERS+ other servers this poll, is escalated.
    The burst subsiding is reported once as `bot_spike_end` so the data
    reflects both edges.
    """
    state = prev_state or "normal"
    if cur_players is None:
        return (state, prev_baseline, cur_players, None)

    # A spike already in progress: substitute the captured baseline so the
    # stored player count stays at its pre-spike level. Wait for the burst to
    # subside, then close it out once -- without re-alerting every poll.
    if state == "spike":
        baseline = prev_baseline if prev_baseline is not None else 0
        if cur_players < BOT_SPIKE_MIN:
            return ("normal", None, cur_players,
                    ("bot_spike_end", "info",
                     f"Player count fell back to {cur_players} -- the "
                     "suspected bot burst has subsided."))
        return ("spike", baseline, baseline, None)

    window = [p for p in recent_players[-BOT_BASELINE_WINDOW:] if p is not None]
    if len(window) < 3:
        return (state, None, cur_players, None)

    # Exclude the freshest polls from the baseline so a burst that is still
    # building cannot raise the baseline it is being measured against.
    edge = max(BOT_SPIKE_POLLS - 1, 0)
    baseline_part = window[:-edge] if edge and len(window) > edge else window
    baseline = statistics.median(baseline_part)
    baseline_store = int(round(baseline))

    # Popular-server lenience: a server with a meaningful historical peak is
    # known to draw a crowd. Scale the burst threshold by its peak so the
    # regular show that fills the room every weekend does not look like a
    # bot fill. Obvious tells below still fire regardless of popularity.
    effective_spike_min = BOT_SPIKE_MIN
    if server_peak is not None and server_peak >= BOT_POPULAR_PEAK_MIN:
        effective_spike_min = max(
            BOT_SPIKE_MIN, int(server_peak * BOT_POPULAR_BURST_FACTOR))

    # Cross-server context: how many *other* servers on this master report
    # exactly the same non-trivial count this poll (copycat), and is the
    # whole network busy right now (organic event vs. one-off bot fill).
    peers = [p for p in (peer_counts or []) if p is not None]
    copycat_peers = (
        sum(1 for p in peers if p == cur_players)
        if cur_players >= BOT_COPYCAT_MIN_COUNT else 0)
    peer_median = statistics.median(peers) if peers else 0
    busy_network = peer_median >= BOT_BUSY_NETWORK_MEDIAN

    # Plateau: a normally-empty server that fills and then holds perfectly
    # flat across many *distinct* master updates. Cached re-reads (same
    # hbcounter as the previous poll) are skipped, because the vanilla
    # Attorney Online master only refreshes each server every few minutes
    # and the bot polls every minute -- without dedup, ~4 of every 5 polls
    # are guaranteed duplicates and a "flat run" is meaningless. The
    # baseline-and-busy-network guards are the same ones the burst path
    # uses, so a server that has been sustainably busy at this count for a
    # while (baseline already non-empty) or a master-wide busy night does
    # not get re-flagged. Popular-server lenience does not apply here -- a
    # popular server that bots fill still satisfies the near-empty baseline
    # rule, so the popular-versus-bot distinction is already covered.
    plateau_pairs = [(cur_players, cur_hbcounter)]
    hbs = list(recent_hbcounters or [None] * len(recent_players))
    for p, hb in zip(reversed(recent_players), reversed(hbs)):
        if p is None:
            continue
        if (hb is not None and plateau_pairs[-1][1] is not None
                and hb == plateau_pairs[-1][1]):
            continue
        plateau_pairs.append((p, hb))
        if len(plateau_pairs) >= BOT_PLATEAU_POLLS:
            break
    if (cur_players >= BOT_PLATEAU_MIN
            and baseline <= BOT_BASELINE_MAX
            and not busy_network
            and len(plateau_pairs) >= BOT_PLATEAU_POLLS
            and all(p == cur_players for p, _ in plateau_pairs)):
        return ("spike", baseline_store, baseline_store,
                ("bot_spike", "alert",
                 f"Player count held flat at {cur_players} across "
                 f"{BOT_PLATEAU_POLLS} fresh master updates on a "
                 f"normally-empty server (baseline ~{baseline:.0f}) -- "
                 "spawned bot clients sit perfectly idle, this is a "
                 "bot-fill plateau."))

    if baseline <= BOT_BASELINE_MAX and cur_players >= effective_spike_min:
        # Look at the previous reading: a real ramp climbs through the
        # baseline band first, so going from near-zero straight to a fully
        # loaded server in a single poll is "instant" rather than "sudden".
        prev = recent_players[-1] if recent_players else None
        instant = (prev is not None and prev <= BOT_INSTANT_MAX_BASELINE
                   and baseline <= BOT_INSTANT_MAX_BASELINE)
        implausible = cur_players >= BOT_IMPLAUSIBLE_MIN
        copycat = copycat_peers >= BOT_COPYCAT_MIN_PEERS

        # Ramp guard: if the previous poll was already well on the way to the
        # current level, this is organic growth (someone advertised the room,
        # a show is starting), not a bot step. Implausible or copycat counts
        # skip the guard -- those shapes are never organic.
        if (not implausible and not copycat and prev is not None
                and prev >= cur_players * BOT_RAMP_RATIO):
            return (state, None, cur_players, None)

        # Busy-network lenience: when the wider network is also lit up, the
        # spike most likely belongs to a real event. Step-shaped fills
        # (instant) and the obvious tells (implausible, copycat) still fire
        # because those shapes do not appear in organic crowds.
        if busy_network and not (instant or implausible or copycat):
            return (state, None, cur_players, None)

        if implausible:
            detail = (f"Player count surged to {cur_players} from a baseline "
                      f"of ~{baseline:.0f} -- {cur_players} concurrent "
                      "players is implausible for this server, this is an "
                      "automated bot fill.")
        elif copycat:
            detail = (f"Player count surged to {cur_players} from a baseline "
                      f"of ~{baseline:.0f}, and {copycat_peers} other "
                      f"server(s) on this master report the exact same "
                      f"{cur_players}-player count this poll -- a copycat "
                      "/ coordinated bot fill, not organic traffic.")
        elif instant:
            detail = (f"Player count jumped from {prev} to {cur_players} in a "
                      f"single poll (baseline ~{baseline:.0f}) -- no organic "
                      "ramp, this is an automated bot fill.")
        else:
            band = ("" if cur_players <= BOT_SPIKE_MAX
                    else f" -- above the usual {BOT_SPIKE_MIN}-"
                         f"{BOT_SPIKE_MAX} band")
            detail = (f"Player count surged to {cur_players} from a baseline "
                      f"of ~{baseline:.0f} within {BOT_SPIKE_POLLS} "
                      f"poll(s){band} -- looks like an automated bot fill, "
                      "not organic traffic.")

        return ("spike", baseline_store, baseline_store,
                ("bot_spike", "alert", detail))
    return (state, None, cur_players, None)
