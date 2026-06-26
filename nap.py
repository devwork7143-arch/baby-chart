"""SweetSpot — predictive next-nap target.

Pure, dependency-free wake-window math. Given the baby's date of birth and the
sleep that just finished, predict when the next nap should start, using
evidence-based wake windows that scale with age plus a sleep-pressure
adjustment (a very short nap means the baby is under-slept, so the next wake
window shrinks to avoid overtiredness).

No Discord or env access lives here — `bot.py` reads `BABY_DOB` and calls
`predict_next_nap` on a successful live `SF`. Edit `WAKE_WINDOWS` to retune.
"""
from datetime import timedelta

# (upper_bound_months_exclusive, (low_min, high_min)) — ordered young→old.
# An age is matched to the first bracket whose upper bound it falls under.
# Ages at/after the last bound get no prediction.
WAKE_WINDOWS = [
    (1, (45, 60)),    # 0–1 month
    (2, (60, 90)),    # 1–2 months
    (5, (75, 120)),   # 2–5 months (covers the 2–3 & 4–5 mo gaps)
    (6, (120, 180)),  # 5–6 months
]
SHORT_NAP_MIN = 45      # a nap shorter than this triggers the sleep-pressure rule
SHORT_NAP_PENALTY = 30  # minutes shaved off the baseline window after a short nap
DAYS_PER_MONTH = 30.4375


def age_months(dob, asof):
    """Baby's age in (fractional) months at `asof` (a datetime)."""
    return (asof.date() - dob).days / DAYS_PER_MONTH


def baseline_window_avg(dob, asof):
    """Average wake window (minutes) for the baby's age, or None if past the table."""
    months = age_months(dob, asof)
    for upper, (lo, hi) in WAKE_WINDOWS:
        if months < upper:
            return (lo + hi) / 2.0
    return None


def predict_next_nap(dob, sleep_end, sleep_duration_min):
    """Target datetime for the next nap, or None when not predictable.

    Returns None when there's no DOB or the baby is past the wake-window table
    (≥ the last bracket's upper bound). Age is anchored at `sleep_end`, so the
    target is `sleep_end + adjusted_wake_window`.
    """
    if dob is None:
        return None
    avg = baseline_window_avg(dob, sleep_end)
    if avg is None:
        return None
    if sleep_duration_min is not None and sleep_duration_min < SHORT_NAP_MIN:
        avg -= SHORT_NAP_PENALTY
    return sleep_end + timedelta(minutes=avg)
