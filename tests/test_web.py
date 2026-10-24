"""The HTML pages, driven like a browser would, against the fake intervals.icu."""
from datetime import date, timedelta

from fastapi.testclient import TestClient

import app.web  # noqa: F401  registers the pages on main.app
from app import main
from tests.test_api import NEW_PLAN, fake  # noqa: F401  (fixture)

FORM = dict(NEW_PLAN, weekly_km="15", runs_per_week="4", long_run_day="saturday")


def test_full_journey(fake):
    c = TestClient(main.app)
    assert "No active plan" in c.get("/").text
    assert "New training plan" in c.get("/plan/new").text

    r = c.post("/plan/new", data=FORM, follow_redirects=False)
    assert r.status_code == 303
    preview = c.get(r.headers["location"]).text
    assert "proposal" in preview and "Week 1" in preview and "Long run" in preview
    plan_url = r.headers["location"]
    assert "waiting for your approval" in c.get("/").text

    c.post(f"{plan_url}/approve")
    assert fake.events
    sessions = main.plan_summary(main.db.connect(), int(plan_url.rsplit("/", 1)[1]))["sessions"]
    first = sessions[0]
    month = c.get("/", params={"month": first["date"][:7]}).text
    assert f"/session/{first['id']}" in month

    new_date = (date.fromisoformat(first["date"]) + timedelta(days=1)).isoformat()
    assert new_date in c.post(f"/session/{first['id']}/move", data={"new_date": new_date}).text
    assert "skipped" in c.post(f"/session/{sessions[1]['id']}/skip").text

    fake.runs = [{"id": "i1", "type": "Run", "start_date_local": f"{sessions[2]['date']}T07:00:00",
                  "distance": 4000, "moving_time": 1560, "average_heartrate": 140}]
    c.post("/sync")
    assert "Your run" in c.get(f"/session/{sessions[2]['id']}").text

    r = c.post("/plan/update", data={"adjust": "distance", "feel": "harder"}, follow_redirects=False)
    assert "What changes" in c.get(r.headers["location"]).text

    c.post(f"{plan_url}/delete")
    assert not fake.events
    assert "No active plan" in c.get("/").text


def test_form_errors_are_shown(fake):
    c = TestClient(main.app)
    soon = dict(FORM, race_date=(date.today() + timedelta(weeks=2)).isoformat())
    assert "weeks away" in c.post("/plan/new", data=soon).text
    assert "Check the times" in c.post("/plan/new", data=dict(FORM, current_5k="abc")).text


def test_stats_page(fake):
    c = TestClient(main.app)
    fake.runs = [{"id": "i9", "type": "Run", "start_date_local": f"{date.today()}T07:00:00", "name": "Park loop",
                  "distance": 5000, "moving_time": 1500, "average_heartrate": 150}]
    c.post("/sync")
    html = c.get("/stats", params={"weeks": 4}).text
    assert "Pace by run type" in html and "Park loop" in html and 'data-run="0"' in html
    assert c.get("/static/chart.umd.min.js").status_code == 200
