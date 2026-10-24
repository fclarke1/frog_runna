import pytest

from app.vdot import fmt_pace, fmt_time, parse_time, paces, predict_time, vdot


@pytest.mark.parametrize("distance_m, time, expected", [
    (5000, "21:30", 45.8),
    (5000, "24:08", 40.0),
    (5000, "19:57", 50.0),
    (21097.5, "1:50:00", 40.4),
])
def test_vdot_matches_daniels_tables(distance_m, time, expected):
    assert vdot(distance_m, parse_time(time)) == pytest.approx(expected, abs=0.5)


def test_predict_time_round_trips():
    for score in (35, 45, 55):
        t = predict_time(score, 10000)
        assert vdot(10000, t) == pytest.approx(score, abs=0.01)


def test_paces_at_vdot_50_match_daniels_table():
    p = paces(50)
    assert p["T"] == pytest.approx(parse_time("4:15"), abs=3)
    assert p["I"] == pytest.approx(parse_time("3:55"), abs=3)
    assert p["R"] == pytest.approx(parse_time("3:40"), abs=4)
    assert p["M"] == pytest.approx(parse_time("4:31"), abs=3)


def test_pace_order():
    p = paces(42)
    assert p["R"] < p["I"] < p["T"] < p["M"] < p["E"][0] < p["E"][1]


def test_formatting():
    assert fmt_pace(310.4) == "5:10"
    assert fmt_time(6600) == "1:50:00"
    assert fmt_time(1290) == "21:30"
    assert parse_time("1:50:00") == 6600
