"""Builds a race training plan. Pure functions, no I/O.

Weeks are 7-day blocks ending on the long-run day. Each week has a target volume,
a phase (base, build, specific, taper) and a fixed set of run slots. Hard
sessions get their structure from the phase. Easy runs fill the remaining volume.
"""
from dataclasses import dataclass
from datetime import date, timedelta

from app.vdot import fmt_pace, paces

# Run slots as (days before the long-run day, role). Hard days never touch.
SLOTS = {
    2: [(4, "q1"), (0, "long")],
    3: [(4, "q1"), (2, "easy"), (0, "long")],
    4: [(6, "easy"), (4, "q1"), (2, "q2"), (0, "long")],
    5: [(6, "easy"), (5, "easy"), (4, "q1"), (2, "q2"), (0, "long")],
}

# Per race distance: peak weekly km, long run share of the week and its cap,
# taper volume factors (last = race week).
RACES = {
    5: {"peak_km": 35, "long_pct": 0.3, "long_cap": 12, "taper": [0.6]},
    10: {"peak_km": 40, "long_pct": 0.33, "long_cap": 14, "taper": [0.6]},
    21.1: {"peak_km": 45, "long_pct": 0.4, "long_cap": 19, "taper": [0.7, 0.5]},
    42.2: {"peak_km": 60, "long_pct": 0.4, "long_cap": 30, "taper": [0.7, 0.5]},
}

WARMUP_KM = 1.5
COOLDOWN_KM = 1.0
MIN_EASY_KM = 3.0
# Pace ranges ("6:44-5:51/km") may arrive on Garmin as a note rather than a target.
# If the spike shows that, set this to False to send single paces instead.
PACE_RANGES = True


@dataclass
class Session:
    date: date
    kind: str  # easy, strides, long, intervals, cruise, tempo, reps, race
    title: str
    distance_km: float
    description: str
    workout_text: str


def race_info(race_km):
    return RACES[min(RACES, key=lambda k: abs(k - race_km))]


def week_ends(start, race_date, long_run_day):
    """Long-run-day dates from the first one on/after start to the one on/after race_date."""
    end = start + timedelta(days=(long_run_day - start.weekday()) % 7)
    ends = []
    while end - timedelta(days=7) < race_date:
        ends.append(end)
        end += timedelta(days=7)
    return ends


def weekly_volumes(n_weeks, start_km, race_km):
    """Target km per week: +10% a week, every 4th week -20%, then the taper."""
    info = race_info(race_km)
    cap = max(info["peak_km"], start_km)
    taper = info["taper"][-n_weeks:] if n_weeks <= len(info["taper"]) else info["taper"]
    level, vols = start_km, []
    for i in range(n_weeks - len(taper)):
        if i > 0 and i % 4 != 3:
            level = min(cap, level * 1.1)
        vols.append(level * 0.8 if i % 4 == 3 else level)
    return vols + [level * f for f in taper]


def phases(n_weeks, race_km):
    n_taper = min(len(race_info(race_km)["taper"]), n_weeks)
    m = n_weeks - n_taper
    n_base = max(1, round(m * 0.3)) if m >= 3 else 0
    n_build = round(m * 0.4)
    return ["base"] * n_base + ["build"] * n_build + ["specific"] * (m - n_base - n_build) + ["taper"] * n_taper


def build_plan(start, race_date, race_km, target_time_s, vdot_score, runs_per_week, long_run_day, start_weekly_km):
    """All sessions from the week containing start through race day.

    The caller filters out dates it doesn't want (e.g. the past, for a plan update).
    """
    p = paces(vdot_score)
    race_pace = target_time_s / race_km
    info = race_info(race_km)
    ends = week_ends(start, race_date, long_run_day)
    vols = weekly_volumes(len(ends), start_weekly_km, race_km)
    week_phases = phases(len(ends), race_km)
    long_race = race_km > 15

    sessions = []
    for i, (end, vol, phase) in enumerate(zip(ends, vols, week_phases)):
        recovery = phase != "taper" and i % 4 == 3
        roles = {}
        for offset, role in SLOTS[runs_per_week]:
            roles[end - timedelta(days=offset)] = role

        week = []
        fixed_km = 0.0
        flexible = []  # (date, kind) for runs sized from what's left
        for day, role in sorted(roles.items()):
            if role == "long":
                long_km = round(min(info["long_cap"], max(5.0, vol * info["long_pct"])), 1)
                if phase == "specific" and long_race:
                    week.append(_long_with_race_pace(day, long_km, p, race_pace))
                else:
                    week.append(_easy(day, long_km, p, kind="long", title="Long run"))
                fixed_km += long_km
            elif role == "easy" or phase == "taper":
                flexible.append((day, "strides" if role == "q1" else "easy"))
            else:
                s = _quality(day, role, phase, i, vol, recovery, long_race, p)
                if s is None:
                    flexible.append((day, "strides" if role == "q1" else "easy"))
                else:
                    week.append(s)
                    fixed_km += s.distance_km

        easy_km = round(max(MIN_EASY_KM, (vol - fixed_km) / len(flexible)), 1) if flexible else 0
        for day, kind in flexible:
            if kind == "strides":
                week.append(_strides(day, easy_km, p))
            else:
                week.append(_easy(day, easy_km, p))

        if end == ends[-1]:
            week = [s for s in week if s.date < race_date]
            week.append(_race(race_date, race_km, race_pace))
        sessions += sorted(week, key=lambda s: s.date)

    return [s for s in sessions if s.date >= start]


def _quality(day, role, phase, week_i, vol, recovery, long_race, p):
    """The hard session for this slot, or None if it should be an easy/strides run."""
    if phase == "base" or (role == "q2" and recovery):
        return None
    if phase == "build":
        return _intervals(day, vol, p) if role == "q1" else _cruise(day, vol, p)
    # specific
    if long_race:
        return _cruise(day, vol, p) if role == "q1" else None
    if role == "q1":
        return _intervals(day, vol, p) if week_i % 2 == 0 else _reps(day, vol, p)
    return _tempo(day, vol, p)


# --- Workout templates ---------------------------------------------------------------

def _target(fast, slow=None):
    if slow is None or not PACE_RANGES:
        return f"{fmt_pace(fast)}/km Pace"
    return f"{fmt_pace(slow)}-{fmt_pace(fast)}/km Pace"


def _e(p):
    return _target(*p["E"])


def _jog_km(minutes, p):
    return minutes * 60 / p["E"][1]


def _reps_count(main_km, rep_km, lo, hi):
    return max(lo, min(hi, round(main_km / rep_km)))


def _structured(day, kind, title, reps, rep_km, pace_s, jog_min, p):
    dist = WARMUP_KM + COOLDOWN_KM + reps * rep_km + (reps - 1) * _jog_km(jog_min, p)
    rep_label = f"{int(rep_km * 1000)}m" if rep_km < 1 else f"{rep_km:g}km"
    rep_step = f"{int(rep_km * 1000)}mtr" if rep_km < 1 else rep_label  # intervals.icu: "m" means minutes
    text = "\n".join([
        "Warmup",
        f"- {WARMUP_KM:g}km {_e(p)}",
        "",
        f"Main set {reps}x",
        f"- {rep_step} {_target(pace_s)}",
        f"- {jog_min:g}m Recovery {_target(p['E'][1])}",
        "",
        "Cooldown",
        f"- {COOLDOWN_KM:g}km {_e(p)}",
    ])
    desc = f"{reps} × {rep_label} at {fmt_pace(pace_s)}/km with {jog_min:g} min easy jog between, plus warm-up and cool-down."
    return Session(day, kind, title, round(dist, 1), desc, text)


def _intervals(day, vol, p):
    # Daniels: I volume ~8% of the week. Low-volume runners still need a minimum to be useful.
    main = vol * 0.12
    rep_km = 1.0 if main >= 4 else 0.8
    reps = _reps_count(main, rep_km, 3, 6)
    return _structured(day, "intervals", "Intervals", reps, rep_km, p["I"], 3 if rep_km == 1 else 2, p)


def _cruise(day, vol, p):
    reps = _reps_count(vol * 0.15, 1.6, 2, 5)
    return _structured(day, "cruise", "Threshold intervals", reps, 1.6, p["T"], 1, p)


def _reps(day, vol, p):
    reps = _reps_count(vol * 0.06, 0.4, 4, 10)
    return _structured(day, "reps", "Repetitions", reps, 0.4, p["R"], 2.5, p)


def _tempo(day, vol, p):
    main = round(max(3.0, min(8.0, vol * 0.15)), 1)
    dist = round(WARMUP_KM + main + COOLDOWN_KM, 1)
    text = "\n".join([
        "Warmup", f"- {WARMUP_KM:g}km {_e(p)}", "",
        "Tempo", f"- {main:g}km {_target(p['T'])}", "",
        "Cooldown", f"- {COOLDOWN_KM:g}km {_e(p)}",
    ])
    return Session(day, "tempo", "Tempo run", dist, f"{main:g} km steady at threshold pace {fmt_pace(p['T'])}/km.", text)


def _easy(day, km, p, kind="easy", title="Easy run"):
    text = f"- {km:g}km {_e(p)}"
    desc = f"{km:g} km at a relaxed, conversational pace ({fmt_pace(p['E'][0])}-{fmt_pace(p['E'][1])}/km)."
    return Session(day, kind, title, km, desc, text)


def _strides(day, km, p):
    text = "\n".join([
        f"- {km:g}km {_e(p)}", "",
        "Strides 6x", f"- 20s {_target(p['R'])}", f"- 60s Recovery {_target(p['E'][1])}",
    ])
    desc = f"{km:g} km easy, then 6 × 20 s quick strides at {fmt_pace(p['R'])}/km with 1 min easy between."
    return Session(day, "strides", "Easy run + strides", round(km + 0.5, 1), desc, text)


def _long_with_race_pace(day, km, p, race_pace):
    fast = round(km / 3, 1)
    easy = round(km - fast, 1)
    text = f"- {easy:g}km {_e(p)}\n- {fast:g}km {_target(race_pace)}"
    desc = f"{easy:g} km easy, then finish with {fast:g} km at goal race pace {fmt_pace(race_pace)}/km."
    return Session(day, "long", "Long run, race-pace finish", km, desc, text)


def _race(day, race_km, race_pace):
    text = f"- {race_km:g}km {_target(race_pace)}"
    return Session(day, "race", "Race day", race_km, f"Race! Target pace {fmt_pace(race_pace)}/km.", text)
