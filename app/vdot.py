"""Jack Daniels' VDOT: fitness score from a race result, and training paces from the score.

Uses the Daniels/Gilbert equations. Speeds are in metres/minute internally; paces
returned to callers are seconds per km.
"""
import math

# Fraction of VO2max each training pace is run at (Daniels' Running Formula).
# Calibrated so the paces match the published VDOT tables to within a few s/km.
PACE_PCT = {
    "E_fast": 0.74,
    "E_slow": 0.62,
    "T": 0.88,
    "I": 0.975,
    "R": 1.05,
}


def _vo2(v):
    """Oxygen cost (ml/kg/min) of running at v metres/minute."""
    return -4.60 + 0.182258 * v + 0.000104 * v * v


def _pct_max(t):
    """Fraction of VO2max sustainable for a race lasting t minutes."""
    return 0.8 + 0.1894393 * math.exp(-0.012778 * t) + 0.2989558 * math.exp(-0.1932605 * t)


def _speed_for_vo2(vo2):
    """Inverse of _vo2: metres/minute that costs vo2."""
    a, b, c = 0.000104, 0.182258, -4.60 - vo2
    return (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)


def vdot(distance_m, time_s):
    t = time_s / 60
    return _vo2(distance_m / t) / _pct_max(t)


def predict_time(score, distance_m):
    """Race time in seconds that corresponds to a VDOT score (bisection)."""
    lo, hi = 60.0, 60.0 * 60 * 10
    for _ in range(100):
        mid = (lo + hi) / 2
        # A longer time gives a lower VDOT.
        if vdot(distance_m, mid) > score:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def paces(score):
    """Training paces in seconds per km for a VDOT score."""
    def pace(pct):
        return 60_000 / _speed_for_vo2(score * pct)

    return {
        "E": (pace(PACE_PCT["E_fast"]), pace(PACE_PCT["E_slow"])),  # (fast, slow)
        "M": predict_time(score, 42195) / 42.195,
        "T": pace(PACE_PCT["T"]),
        "I": pace(PACE_PCT["I"]),
        "R": pace(PACE_PCT["R"]),
    }


def fmt_pace(sec_per_km):
    """310.4 -> '5:10'."""
    s = round(sec_per_km)
    return f"{s // 60}:{s % 60:02d}"


def fmt_time(seconds):
    """6600 -> '1:50:00', 1290 -> '21:30'."""
    s = round(seconds)
    h, m, s = s // 3600, s // 60 % 60, s % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def parse_time(text):
    """'21:30' or '1:50:00' -> seconds."""
    total = 0
    for part in text.strip().split(":"):
        total = total * 60 + int(part)
    return total
