"""Heartbeat-counter analysis.

A server's hbcounter rises by roughly 1 per minute. When it reaches HB_CAP it
rolls over and continues from (HB_CAP - ROLLOVER_DROP); that is normal. Any
change the rate model and rollover cannot explain is flagged.
"""
from config import HB_CAP, HB_MARGIN, HB_RATE_MAX, ROLLOVER_DROP


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

    # The counter decreased: either a normal rollover at HB_CAP, or a drop
    # that nothing in the rate model can account for.
    real_gain = delta + ROLLOVER_DROP            # value if one rollover happened
    could_roll = (prev_hb + expected_max) >= HB_CAP
    if could_roll and (-HB_MARGIN <= real_gain <= expected_max):
        return ("hb_rollover", "info",
                f"HB rolled over {prev_hb} -> {cur_hb} "
                f"(normal {HB_CAP // 1000}k reset).")

    sev = "alert" if reliable else "info"
    note = "" if reliable else " (long polling gap -- unverified)"
    return ("hb_drop", sev,
            f"HB DROPPED {prev_hb} -> {cur_hb} ({delta}) -- "
            f"not explained by a normal rollover{note}.")
