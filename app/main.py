"""Frog Runna back end: JSON API over the planner, the SQLite store and intervals.icu.

Every plan change goes through a proposal: create or update -> preview -> approve.
"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app import db, intervals
from app.planner import build_plan
from app.vdot import fmt_pace, fmt_time, paces, parse_time, predict_time, vdot

log = logging.getLogger("frog_runna")

DISTANCES = {"5k": 5.0, "10k": 10.0, "half": 21.1, "marathon": 42.2}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
FEEL_ADJUST = {"much_easier": 2, "easier": 1, "harder": -1, "much_harder": -2}  # VDOT points
FEEL_DISTANCE = {"much_easier": 1.2, "easier": 1.1, "harder": 0.9, "much_harder": 0.8}  # weekly km factor
MIN_WEEKS, MAX_WEEKS = 6, 24
SYNC_EVERY_S = 30 * 60


@asynccontextmanager
async def lifespan(app):
    task = asyncio.create_task(_sync_loop())
    yield
    task.cancel()


app = FastAPI(title="Frog Runna", lifespan=lifespan)


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def today():
    return date.today()


# --- Plans ------------------------------------------------------------------------------

class NewPlan(BaseModel):
    race_name: str
    race_distance: Literal["5k", "10k", "half", "marathon"]
    race_date: date
    target_time: str  # "1:50:00" or "21:30"
    current_5k: str  # "25:00"
    weekly_km: float = 15
    runs_per_week: int = 4
    long_run_day: str = "saturday"


class PlanUpdate(BaseModel):
    adjust: Literal["pace", "distance"] = "pace"
    feel: Literal["much_easier", "easier", "harder", "much_harder"] | None = None
    new_5k: str | None = None  # a recent 5k time, instead of feel (pace only)


@app.post("/api/plans")
def new_plan(body: NewPlan, conn=Depends(get_conn)):
    start = today() + timedelta(days=1)
    weeks = (body.race_date - start).days / 7
    if not MIN_WEEKS <= weeks <= MAX_WEEKS:
        raise HTTPException(400, f"Race must be {MIN_WEEKS}-{MAX_WEEKS} weeks away (it is {weeks:.1f}).")
    if body.runs_per_week not in (2, 3, 4, 5):
        raise HTTPException(400, "runs_per_week must be 2-5.")
    if body.long_run_day.lower() not in DAYS:
        raise HTTPException(400, f"long_run_day must be one of {DAYS}.")
    plan = {
        "race_name": body.race_name,
        "race_km": DISTANCES[body.race_distance],
        "race_date": body.race_date.isoformat(),
        "target_time_s": parse_time(body.target_time),
        "vdot": round(vdot(5000, parse_time(body.current_5k)), 1),
        "runs_per_week": body.runs_per_week,
        "long_run_day": DAYS.index(body.long_run_day.lower()),
        "start_date": start.isoformat(),
        "start_weekly_km": body.weekly_km,
    }
    return {"plan_id": _propose(conn, plan, replaces=None)}


@app.post("/api/plans/update")
def update_plan(body: PlanUpdate, conn=Depends(get_conn)):
    active = _active_plan(conn)
    if not active:
        raise HTTPException(404, "No active plan.")
    plan = {k: active[k] for k in ("race_name", "race_km", "race_date", "target_time_s", "vdot", "runs_per_week",
                                   "long_run_day", "start_date", "start_weekly_km")}
    if body.adjust == "distance":
        if not body.feel:
            raise HTTPException(400, "Choose how the sessions are feeling.")
        # The plan is rebuilt from its first week, so scaling the start scales every week (up to the peak cap).
        plan["start_weekly_km"] = round(active["start_weekly_km"] * FEEL_DISTANCE[body.feel], 1)
    elif body.new_5k:
        plan["vdot"] = round(vdot(5000, parse_time(body.new_5k)), 1)
    elif body.feel:
        plan["vdot"] = active["vdot"] + FEEL_ADJUST[body.feel]
    else:
        raise HTTPException(400, "Give either feel or new_5k.")
    return {"plan_id": _propose(conn, plan, replaces=active["id"])}


def _propose(conn, plan, replaces):
    """Save a proposed plan with its sessions from tomorrow on. Returns the plan id."""
    conn.execute("DELETE FROM sessions WHERE plan_id IN (SELECT id FROM plans WHERE status = 'proposed')")
    conn.execute("DELETE FROM plans WHERE status = 'proposed'")
    sessions = build_plan(
        date.fromisoformat(plan["start_date"]), date.fromisoformat(plan["race_date"]), plan["race_km"],
        plan["target_time_s"], plan["vdot"], plan["runs_per_week"], plan["long_run_day"],
        plan["start_weekly_km"],
    )
    cur = conn.execute(
        """INSERT INTO plans (status, replaces_plan_id, race_name, race_km, race_date, target_time_s, vdot,
                              runs_per_week, long_run_day, start_date, start_weekly_km)
           VALUES ('proposed', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (replaces, plan["race_name"], plan["race_km"], plan["race_date"], plan["target_time_s"], plan["vdot"],
         plan["runs_per_week"], plan["long_run_day"], plan["start_date"], plan["start_weekly_km"]),
    )
    plan_id = cur.lastrowid
    conn.executemany(
        """INSERT INTO sessions (plan_id, date, kind, title, distance_km, description, workout_text)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [(plan_id, s.date.isoformat(), s.kind, s.title, s.distance_km, s.description, s.workout_text)
         for s in sessions if s.date > today()],
    )
    conn.commit()
    return plan_id


@app.get("/api/plans/active")
def active_plan(conn=Depends(get_conn)):
    plan = _active_plan(conn)
    if not plan:
        raise HTTPException(404, "No active plan.")
    return plan_summary(conn, plan["id"])


@app.get("/api/plans/{plan_id}")
def get_plan(plan_id: int, conn=Depends(get_conn)):
    return plan_summary(conn, plan_id)


def plan_summary(conn, plan_id):
    """Everything the preview page needs: fitness vs target, paces, sessions and (for updates) changes."""
    plan = db.row(conn, "SELECT * FROM plans WHERE id = ?", plan_id)
    if not plan:
        raise HTTPException(404, "Plan not found.")
    race_m = plan["race_km"] * 1000
    target_vdot = vdot(race_m, plan["target_time_s"])
    weeks_left = max(1, (date.fromisoformat(plan["race_date"]) - today()).days / 7)
    gain_per_month = (target_vdot - plan["vdot"]) / (weeks_left / 4.35)
    p = paces(plan["vdot"])
    sessions = db.rows(conn, "SELECT * FROM sessions WHERE plan_id = ? ORDER BY date", plan_id)
    result = {
        "plan": plan,
        "current_vdot": plan["vdot"],
        "target_vdot": round(target_vdot, 1),
        "predicted_race_time": fmt_time(predict_time(plan["vdot"], race_m)),
        "target_race_time": fmt_time(plan["target_time_s"]),
        "feasibility": "realistic" if gain_per_month <= 1 else "ambitious" if gain_per_month <= 2 else "unlikely",
        "paces_per_km": {
            "easy": f"{fmt_pace(p['E'][0])}-{fmt_pace(p['E'][1])}",
            "marathon": fmt_pace(p["M"]),
            "threshold": fmt_pace(p["T"]),
            "interval": fmt_pace(p["I"]),
            "repetition": fmt_pace(p["R"]),
            "race_target": fmt_pace(plan["target_time_s"] / plan["race_km"]),
        },
        "sessions": sessions,
    }
    if plan["status"] == "proposed" and plan["replaces_plan_id"]:
        result["changes"] = _changes(conn, plan["replaces_plan_id"], sessions)
    return result


def _changes(conn, old_plan_id, new_sessions):
    old = {s["date"]: s for s in db.rows(
        conn, "SELECT * FROM sessions WHERE plan_id = ? AND status = 'planned' AND date > ?",
        old_plan_id, today().isoformat())}
    new = {s["date"]: s for s in new_sessions}
    changes = []
    for d in sorted(old.keys() | new.keys()):
        o, n = old.get(d), new.get(d)
        if (o and o["description"]) != (n and n["description"]):
            changes.append({"date": d, "old": o and o["description"], "new": n and n["description"]})
    return changes


@app.post("/api/plans/{plan_id}/approve")
def approve_plan(plan_id: int, conn=Depends(get_conn)):
    plan = db.row(conn, "SELECT * FROM plans WHERE id = ?", plan_id)
    if not plan or plan["status"] != "proposed":
        raise HTTPException(404, "No proposed plan with that id.")
    old = _active_plan(conn)
    if old:
        # Future sessions of the old plan are replaced. Past, done and skipped ones stay as history.
        for s in db.rows(conn, "SELECT * FROM sessions WHERE plan_id = ? AND status = 'planned' AND date > ?",
                         old["id"], today().isoformat()):
            if s["intervals_event_id"]:
                intervals.delete_event(s["intervals_event_id"])
            conn.execute("DELETE FROM sessions WHERE id = ?", (s["id"],))
            conn.commit()
        conn.execute("UPDATE plans SET status = 'archived' WHERE id = ?", (old["id"],))
    conn.execute("UPDATE plans SET status = 'active' WHERE id = ?", (plan_id,))
    conn.commit()
    pushed = push_pending(conn)
    return {"plan_id": plan_id, "status": "active", "pushed": pushed}


@app.delete("/api/plans/{plan_id}")
def delete_plan(plan_id: int, conn=Depends(get_conn)):
    """Delete a plan of any status, removing its workouts from intervals.icu.

    Runs stay in the activities table; ones that matched a session become unplanned runs.
    """
    if not db.row(conn, "SELECT id FROM plans WHERE id = ?", plan_id):
        raise HTTPException(404, "Plan not found.")
    pushed = db.rows(conn, "SELECT id, intervals_event_id FROM sessions WHERE plan_id = ? "
                           "AND intervals_event_id IS NOT NULL", plan_id)
    for s in pushed:
        intervals.delete_event(s["intervals_event_id"])
        conn.execute("UPDATE sessions SET intervals_event_id = NULL WHERE id = ?", (s["id"],))
        conn.commit()  # per session, so a failure part way can just be retried
    # Proposals to update this plan make no sense once it's gone.
    doomed = [plan_id] + [p["id"] for p in db.rows(conn, "SELECT id FROM plans WHERE replaces_plan_id = ?", plan_id)]
    for pid in doomed:
        conn.execute("UPDATE activities SET session_id = NULL WHERE session_id IN "
                     "(SELECT id FROM sessions WHERE plan_id = ?)", (pid,))
        conn.execute("DELETE FROM sessions WHERE plan_id = ?", (pid,))
        conn.execute("DELETE FROM plans WHERE id = ?", (pid,))
    conn.commit()
    return {"deleted": plan_id, "workouts_removed": len(pushed)}


def _active_plan(conn):
    return db.row(conn, "SELECT * FROM plans WHERE status = 'active'")


# --- Sessions ---------------------------------------------------------------------------

class Move(BaseModel):
    date: date


@app.get("/api/sessions")
def list_sessions(start: date, end: date, conn=Depends(get_conn)):
    """Sessions of active and past plans, plus runs, in a date range (the calendar's data)."""
    sessions = db.rows(conn, """
        SELECT s.*, a.distance_km AS actual_km, a.moving_time_s AS actual_time_s, a.avg_hr AS actual_avg_hr
        FROM sessions s JOIN plans p ON p.id = s.plan_id
        LEFT JOIN activities a ON a.id = s.activity_id
        WHERE p.status IN ('active', 'archived') AND s.date BETWEEN ? AND ? ORDER BY s.date""",
        start.isoformat(), end.isoformat())
    for s in sessions:
        if s["actual_km"]:
            s["actual_pace"] = fmt_pace(s["actual_time_s"] / s["actual_km"])
    unmatched = db.rows(conn, "SELECT * FROM activities WHERE session_id IS NULL AND date BETWEEN ? AND ?",
                        start.isoformat(), end.isoformat())
    return {"sessions": sessions, "unplanned_runs": unmatched}


@app.post("/api/sessions/{session_id}/move")
def move_session(session_id: int, body: Move, conn=Depends(get_conn)):
    s = _editable_session(conn, session_id)
    if s["intervals_event_id"]:
        intervals.move_event(s["intervals_event_id"], body.date.isoformat())
    conn.execute("UPDATE sessions SET date = ? WHERE id = ?", (body.date.isoformat(), session_id))
    conn.commit()
    return db.row(conn, "SELECT * FROM sessions WHERE id = ?", session_id)


@app.post("/api/sessions/{session_id}/skip")
def skip_session(session_id: int, conn=Depends(get_conn)):
    s = _editable_session(conn, session_id)
    if s["intervals_event_id"]:
        intervals.delete_event(s["intervals_event_id"])
    conn.execute("UPDATE sessions SET status = 'skipped', intervals_event_id = NULL WHERE id = ?", (session_id,))
    conn.commit()
    return db.row(conn, "SELECT * FROM sessions WHERE id = ?", session_id)


def _editable_session(conn, session_id):
    s = db.row(conn, """SELECT s.* FROM sessions s JOIN plans p ON p.id = s.plan_id
                        WHERE s.id = ? AND p.status = 'active'""", session_id)
    if not s or s["status"] != "planned":
        raise HTTPException(404, "No planned session with that id in the active plan.")
    return s


# --- Sync -------------------------------------------------------------------------------

@app.post("/api/sync")
def sync(conn=Depends(get_conn)):
    pushed = push_pending(conn)
    fetched, matched = pull_runs(conn)
    return {"pushed": pushed, "runs_fetched": fetched, "runs_matched": matched}


def push_pending(conn):
    """Send active-plan sessions from today on that aren't on intervals.icu yet. Returns the count."""
    pending = db.rows(conn, """
        SELECT s.* FROM sessions s JOIN plans p ON p.id = s.plan_id
        WHERE p.status = 'active' AND s.status = 'planned' AND s.intervals_event_id IS NULL AND s.date >= ?""",
        today().isoformat())
    for s in pending:
        event_id = intervals.create_event(s["date"], s["title"], s["workout_text"])
        conn.execute("UPDATE sessions SET intervals_event_id = ? WHERE id = ?", (event_id, s["id"]))
        conn.commit()  # per session, so a failure part way doesn't cause duplicates on retry
    return len(pending)


def pull_runs(conn, days=30):
    """Fetch recent runs, store them, and mark the planned session they belong to as done."""
    runs = intervals.fetch_runs(today() - timedelta(days=days), today())
    for a in runs:
        conn.execute(
            """INSERT INTO activities (id, date, name, distance_km, moving_time_s, avg_hr) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET date = excluded.date, name = excluded.name,
                   distance_km = excluded.distance_km, moving_time_s = excluded.moving_time_s,
                   avg_hr = excluded.avg_hr""",
            (str(a["id"]), a["start_date_local"][:10], a.get("name"), round((a.get("distance") or 0) / 1000, 2),
             a.get("moving_time"), a.get("average_heartrate")),
        )
    matched = 0
    for a in db.rows(conn, "SELECT * FROM activities WHERE session_id IS NULL"):
        s = _session_for_run(conn, a["date"])
        if s:
            conn.execute("UPDATE activities SET session_id = ? WHERE id = ?", (s["id"], a["id"]))
            conn.execute("UPDATE sessions SET status = 'done', activity_id = ? WHERE id = ?", (a["id"], s["id"]))
            matched += 1
    conn.commit()
    return len(runs), matched


def _session_for_run(conn, run_date):
    """The planned session on the run's date, else the nearest one a day either side."""
    d = date.fromisoformat(run_date)
    for candidate in (d, d - timedelta(days=1), d + timedelta(days=1)):
        s = db.row(conn, """SELECT s.* FROM sessions s JOIN plans p ON p.id = s.plan_id
                            WHERE p.status IN ('active', 'archived') AND s.status = 'planned' AND s.date = ?""",
                   candidate.isoformat())
        if s:
            return s
    return None


def _sync_once():
    conn = db.connect()
    try:
        push_pending(conn)
        pull_runs(conn)
    finally:
        conn.close()


async def _sync_loop():
    if not os.environ.get("INTERVALS_API_KEY"):
        log.warning("INTERVALS_API_KEY not set; background sync disabled.")
        return
    while True:
        try:
            await asyncio.to_thread(_sync_once)
        except Exception:
            log.exception("Background sync failed")
        await asyncio.sleep(SYNC_EVERY_S)
