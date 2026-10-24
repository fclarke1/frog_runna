from collections import defaultdict
from datetime import date, timedelta

import pytest

from app.planner import build_plan, weekly_volumes
from app.vdot import parse_time, vdot

START = date(2026, 10, 26)  # a Monday
HARD = {"intervals", "cruise", "tempo", "reps", "long", "race"}


def plan(race_km=21.1, race_date=date(2027, 2, 13), runs=4, long_day=5, target="1:50:00"):
    return build_plan(START, race_date, race_km, parse_time(target), vdot(5000, parse_time("25:00")),
                      runs, long_day, 15)


@pytest.mark.parametrize("race_km", [5, 10, 21.1, 42.2])
@pytest.mark.parametrize("runs", [2, 3, 4, 5])
def test_runs_per_week_and_long_run_day(race_km, runs):
    sessions = plan(race_km=race_km, runs=runs)
    weeks = defaultdict(list)
    for s in sessions:
        weeks[(s.date - START).days // 7].append(s)
    for i, week in weeks.items():
        if i == max(weeks):  # race week is cut short at the race
            continue
        assert len(week) == runs
        assert [s for s in week if s.kind == "long"][0].date.weekday() == 5


@pytest.mark.parametrize("runs", [2, 3, 4, 5])
def test_no_hard_days_back_to_back(runs):
    hard_days = sorted(s.date for s in plan(runs=runs) if s.kind in HARD)
    for a, b in zip(hard_days, hard_days[1:]):
        assert b - a > timedelta(days=1)


def test_one_session_per_day_and_race_last():
    sessions = plan(race_date=date(2027, 2, 11))  # Thursday race, not the long-run day
    dates = [s.date for s in sessions]
    assert len(dates) == len(set(dates))
    assert sessions[-1].kind == "race" and sessions[-1].date == date(2027, 2, 11)
    assert all(s.date >= START for s in sessions)


def test_volume_ramp_and_taper():
    vols = weekly_volumes(16, 15, 21.1)
    peak = vols[0]
    for v in vols[1:-2]:
        assert v <= peak * 1.1 + 1e-9
        peak = max(peak, v)
    assert vols[-1] < vols[-2] < max(vols)


def test_workouts_have_text():
    for s in plan(race_km=5):
        assert s.workout_text.startswith(("- ", "Warmup"))
        assert s.distance_km > 0


def test_short_plan_still_builds():
    sessions = plan(race_date=START + timedelta(days=20))
    assert sessions[-1].kind == "race"
