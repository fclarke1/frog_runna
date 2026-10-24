"""API flows end to end against a temp database and a fake intervals.icu."""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app import db, intervals, main


class FakeIntervals:
    def __init__(self):
        self.events, self.runs, self.next_id = {}, [], 1

    def create_event(self, d, name, text):
        self.next_id += 1
        self.events[self.next_id] = {"date": d, "name": name, "text": text}
        return self.next_id

    def move_event(self, event_id, d):
        self.events[event_id]["date"] = d

    def delete_event(self, event_id):
        self.events.pop(event_id, None)

    def fetch_runs(self, oldest, newest):
        return self.runs

    def fetch_work_pace(self, activity_id):
        return None


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "test.db"))
    f = FakeIntervals()
    for name in ("create_event", "move_event", "delete_event", "fetch_runs", "fetch_work_pace"):
        monkeypatch.setattr(intervals, name, getattr(f, name))
    return f


@pytest.fixture
def client(fake):
    return TestClient(main.app)


NEW_PLAN = {
    "race_name": "Spring Half",
    "race_distance": "half",
    "race_date": (date.today() + timedelta(weeks=14)).isoformat(),
    "target_time": "1:50:00",
    "current_5k": "25:00",
    "weekly_km": 15,
    "runs_per_week": 4,
    "long_run_day": "Saturday",
}


def approved_plan(client):
    plan_id = client.post("/api/plans", json=NEW_PLAN).json()["plan_id"]
    assert client.post(f"/api/plans/{plan_id}/approve").status_code == 200
    return plan_id


def test_new_plan_preview_and_approve_pushes_everything(client, fake):
    r = client.post("/api/plans", json=NEW_PLAN)
    assert r.status_code == 200
    preview = client.get(f"/api/plans/{r.json()['plan_id']}").json()
    assert preview["plan"]["status"] == "proposed"
    assert preview["target_vdot"] == pytest.approx(40.4, abs=0.1)
    assert preview["feasibility"] in ("realistic", "ambitious", "unlikely")
    assert not fake.events  # nothing pushed before approval

    approved = client.post(f"/api/plans/{r.json()['plan_id']}/approve").json()
    assert approved["pushed"] == len(preview["sessions"]) == len(fake.events)


def test_race_too_soon_is_rejected(client):
    body = dict(NEW_PLAN, race_date=(date.today() + timedelta(weeks=3)).isoformat())
    assert client.post("/api/plans", json=body).status_code == 400


def test_update_needs_approval_then_replaces_future_sessions(client, fake):
    old_id = approved_plan(client)
    old_events = set(fake.events)
    r = client.post("/api/plans/update", json={"feel": "easier"})
    proposal = client.get(f"/api/plans/{r.json()['plan_id']}").json()
    assert proposal["current_vdot"] == pytest.approx(proposal["plan"]["vdot"])
    assert proposal["changes"]  # faster paces change the descriptions
    assert set(fake.events) == old_events  # untouched until approved

    client.post(f"/api/plans/{r.json()['plan_id']}/approve")
    assert not old_events & set(fake.events)
    assert len(fake.events) == len(proposal["sessions"])
    assert client.get(f"/api/plans/{old_id}").json()["plan"]["status"] == "archived"


def test_delete_active_plan_removes_workouts_and_keeps_runs(client, fake):
    plan_id = approved_plan(client)
    first = client.get(f"/api/plans/{plan_id}").json()["sessions"][0]
    fake.runs = [{"id": "i42", "type": "Run", "start_date_local": f"{first['date']}T07:00:00",
                  "distance": 5000, "moving_time": 1800}]
    client.post("/api/sync")
    proposal_id = client.post("/api/plans/update", json={"feel": "harder"}).json()["plan_id"]

    r = client.delete(f"/api/plans/{plan_id}").json()
    assert r["workouts_removed"] > 0 and not fake.events
    assert client.get(f"/api/plans/{plan_id}").status_code == 404
    assert client.get(f"/api/plans/{proposal_id}").status_code == 404
    day = date.fromisoformat(first["date"])
    runs = client.get("/api/sessions", params={"start": day, "end": day}).json()
    assert runs["sessions"] == [] and runs["unplanned_runs"][0]["id"] == "i42"
    assert client.delete(f"/api/plans/{plan_id}").status_code == 404


def test_update_distance_keeps_paces_and_adds_km(client, fake):
    plan_id = approved_plan(client)
    before = client.get(f"/api/plans/{plan_id}").json()
    r = client.post("/api/plans/update", json={"adjust": "distance", "feel": "easier"})
    after = client.get(f"/api/plans/{r.json()['plan_id']}").json()
    assert after["paces_per_km"] == before["paces_per_km"]
    assert after["plan"]["start_weekly_km"] == 16.5
    first_week = lambda p: sum(s["distance_km"] for s in p["sessions"][:4])
    assert first_week(after) > first_week(before)
    assert after["changes"]
    assert client.post("/api/plans/update", json={"adjust": "distance"}).status_code == 400


def test_update_with_new_5k_and_discard(client, fake):
    approved_plan(client)
    plan_id = client.post("/api/plans/update", json={"new_5k": "23:30"}).json()["plan_id"]
    assert client.get(f"/api/plans/{plan_id}").json()["current_vdot"] > 40
    assert client.delete(f"/api/plans/{plan_id}").status_code == 200
    assert client.get(f"/api/plans/{plan_id}").status_code == 404


def test_move_and_skip(client, fake):
    plan_id = approved_plan(client)
    s1, s2 = client.get(f"/api/plans/{plan_id}").json()["sessions"][:2]
    new_date = (date.fromisoformat(s1["date"]) + timedelta(days=1)).isoformat()
    assert client.post(f"/api/sessions/{s1['id']}/move", json={"date": new_date}).json()["date"] == new_date
    assert fake.events[s1["intervals_event_id"]]["date"] == new_date

    skipped = client.post(f"/api/sessions/{s2['id']}/skip").json()
    assert skipped["status"] == "skipped"
    assert s2["intervals_event_id"] not in fake.events


def test_sync_matches_run_to_session(client, fake):
    plan_id = approved_plan(client)
    first = client.get(f"/api/plans/{plan_id}").json()["sessions"][0]
    fake.runs = [{"id": "i42", "type": "Run", "start_date_local": f"{first['date']}T07:00:00", "name": "Morning Run",
                  "distance": 5200, "moving_time": 1820, "average_heartrate": 148}]
    assert client.post("/api/sync").json()["runs_matched"] == 1
    day = date.fromisoformat(first["date"])
    sessions = client.get("/api/sessions", params={"start": day, "end": day}).json()["sessions"]
    assert sessions[0]["status"] == "done"
    assert sessions[0]["actual_km"] == 5.2
    assert sessions[0]["actual_pace"] == "5:50"


def test_stats_weekly_totals_and_paces(client, fake, monkeypatch):
    plan_id = approved_plan(client)
    sessions = client.get(f"/api/plans/{plan_id}").json()["sessions"]
    quality = next(s for s in sessions if s["kind"] in main.QUALITY_KINDS)
    monkeypatch.setattr(main, "today", lambda: date.fromisoformat(quality["date"]))
    monkeypatch.setattr(intervals, "fetch_work_pace", lambda _id: 280.0)
    fake.runs = [
        {"id": "q", "type": "Run", "start_date_local": f"{quality['date']}T07:00:00", "distance": 6000,
         "moving_time": 2100, "average_cadence": 80},
        {"id": "u", "type": "Run", "start_date_local": "2020-01-01T07:00:00", "distance": 1000, "moving_time": 400},
    ]
    client.post("/api/sync")
    s = client.get("/api/stats", params={"weeks": 4}).json()
    this_week = s["weeks"][-1]
    assert len(s["weeks"]) == 4 and s["total_runs"] == 1  # the 2020 run is outside the range
    assert this_week["km"] == 6.0 and this_week["runs"] == 1 and this_week["planned_runs"] >= 1
    group = main.PACE_GROUPS[quality["kind"]]
    assert this_week["paces"] == {group: 280.0}  # hard-part pace, not the 350 s/km average
    run = s["runs"][0]
    assert run["cadence_spm"] == 160 and run["type"] == group and run["session_title"] == quality["title"]
