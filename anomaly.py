"""Heartbeat-counter and player-count analysis.

A server's hbcounter rises by roughly 1 per minute. When it reaches HB_CAP it
rolls over and continues from (HB_CAP - ROLLOVER_DROP); that is normal. Any
change the rate model and rollover cannot explain is flagged.

The player count is watched separately: a near-empty server that suddenly
fills with players over a poll or two is flagged as a suspected bot pattern.
"""
import statistics

from config import (BOT_BASELINE_MAX, BOT_BASELINE_WINDOW, BOT_SPIKE_MAX,
                    BOT_SPIKE_MIN, BOT_SPIKE_POLLS, HB_CAP, HB_MARGIN,
                    HB_RATE_MAX, HB_REAL_RESTART_MINUTES, HB_RESTART_WINDOW,
                    ROLLOVER_DROP)


def analyze_hb(prev_hb, cur_hb, elapsed_min, reliable=True):
    """Compare a server's heartbeat counter between two polls.

    Returns (type, severity, detail), or None when the change looks normal.
    `reliable` should be False when the gap between polls is unusually long or
    the server had been missing -- in that case findings are downgraded to info.
    """
    if prev_hb is None or cur_hb is None:
        return None

    elapsed_min = max(elapsed_min, 0.5)
    expected_max = elapsed_min * HB_RATE_MAX + HB_MARGIN
    delta = cur_hb - prev_hb

    if delta >= 0:
        if delta > expected_max:
            sev = "alert" if reliable else "info"
            return ("hb_jump", sev,
                    f"HB jumped +{delta} in {elapsed_min:.1f} min "
                    f"(expected at most +{expected_max:.0f}).")
        return None

    # The counter decreased: a normal rollover at HB_CAP, a fresh restart, or a
    # drop that nothing in the rate model can account for.
    real_gain = delta + ROLLOVER_DROP            # value if one rollover happened
    could_roll = (prev_hb + expected_max) >= HB_CAP
    if could_roll and (-HB_MARGIN <= real_gain <= expected_max):
        return ("hb_rollover", "info",
                f"HB rolled over {prev_hb} -> {cur_hb} "
                f"(normal {HB_CAP // 1000}k reset).")

    # A counter that has fallen to the floor (<= HB_RESTART_WINDOW) usually
    # means the server was taken down and brought back. But a genuine restart
    # takes time: the master list holds a dead entry for ~30 min, so a real
    # down-and-back cycle leaves a long gap since the last reading. If the
    # counter slammed to the floor with less than HB_REAL_RESTART_MINUTES
    # elapsed, the server never had time to actually go down -- that points to
    # the counter being reset by hand.
    if 0 <= cur_hb <= HB_RESTART_WINDOW:
        if elapsed_min < HB_REAL_RESTART_MINUTES:
            return ("hb_reset", "alert",
                    f"HB slammed {prev_hb} -> {cur_hb} after only "
                    f"{elapsed_min:.1f} min -- too fast for a genuine restart "
                    f"({HB_REAL_RESTART_MINUTES} min), likely a manual reset.")
        return ("hb_restart", "info",
                f"HB reset {prev_hb} -> {cur_hb} -- server looks restarted "
                f"(counter within restart range after a {elapsed_min:.0f} min "
                f"gap).")

    sev = "alert" if reliable else "info"
    note = "" if reliable else " (long polling gap -- unverified)"
    return ("hb_drop", sev,
            f"HB DROPPED {prev_hb} -> {cur_hb} ({delta}) -- "
            f"not explained by a normal rollover{note}.")


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
