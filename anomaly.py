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

from config import (BOT_BASELINE_MAX, BOT_BASELINE_WINDOW, BOT_SPIKE_MAX,
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
    # the floor with less than real_restart_minutes elapsed, the server never
    # had time to actually go down -- that points to a hand-reset counter.
    if 0 <= cur_hb <= restart_window:
        if elapsed_min < real_restart_minutes:
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


def analyze_players(recent_players, cur_players, prev_state="normal"):
    """Spot a bot-like player burst on a normally near-empty server.

    `recent_players` is the server's player counts from prior polls,
    oldest-first and excluding the current poll. `cur_players` is this poll's
    count. `prev_state` is the server's stored bot-pattern state -- "normal"
    or "spike".

    Returns (new_state, anomaly) where anomaly is (type, severity, detail) or
    None. A sudden jump to BOT_SPIKE_MIN+ players from a baseline at or below
    BOT_BASELINE_MAX is reported as `bot_spike`; the burst subsiding is
    reported once as `bot_spike_end` so the data reflects both edges.
    """
    state = prev_state or "normal"
    if cur_players is None:
        return state, None

    # A spike already in progress: wait for the burst to subside, then close
    # it out once -- without re-alerting on every poll while it persists.
    if state == "spike":
        if cur_players < BOT_SPIKE_MIN:
            return ("normal", ("bot_spike_end", "info",
                    f"Player count fell back to {cur_players} -- the "
                    "suspected bot burst has subsided."))
        return ("spike", None)

    window = [p for p in recent_players[-BOT_BASELINE_WINDOW:] if p is not None]
    if len(window) < 3:
        return (state, None)

    # Exclude the freshest polls from the baseline so a burst that is still
    # building cannot raise the baseline it is being measured against.
    edge = max(BOT_SPIKE_POLLS - 1, 0)
    baseline_part = window[:-edge] if edge and len(window) > edge else window
    baseline = statistics.median(baseline_part)

    if baseline <= BOT_BASELINE_MAX and cur_players >= BOT_SPIKE_MIN:
        band = ("" if cur_players <= BOT_SPIKE_MAX
                else f" -- above the usual {BOT_SPIKE_MIN}-{BOT_SPIKE_MAX} band")
        return ("spike", ("bot_spike", "alert",
                f"Player count surged to {cur_players} from a baseline of "
                f"~{baseline:.0f} within {BOT_SPIKE_POLLS} poll(s){band} -- "
                "looks like an automated bot fill, not organic traffic."))
    return (state, None)
