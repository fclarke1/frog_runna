"""HTML pages. Each page calls the same functions as the JSON API in main.py.

Run with `uvicorn app.web:app`: importing this module adds the pages to main's app.
"""
import calendar
import os
from collections import defaultdict
from datetime import date, timedelta

from fastapi import Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db
from app.main import (DAYS, Move, NewPlan, PlanUpdate, _active_plan, app, approve_plan, delete_plan, get_conn,
                      list_sessions, move_session, new_plan, plan_summary, skip_session, stats, sync, today, update_plan)
from app.vdot import fmt_pace, fmt_time, paces

HERE = os.path.dirname(__file__)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.globals.update(fmt_pace=fmt_pace, fmt_time=fmt_time, DAYS=DAYS)
templates.env.filters["nice_date"] = lambda d: date.fromisoformat(str(d)).strftime("%a %-d %b")


def redirect(url):
    return RedirectResponse(url, status_code=303)


def page(request, name, **context):
    return templates.TemplateResponse(request, name, context)


# --- Calendar -------------------------------------------------------------------------

@app.get("/")
def calendar_page(request: Request, month: str | None = None, conn=Depends(get_conn)):
    first = date.fromisoformat(f"{month}-01") if month else today().replace(day=1)
    weeks = calendar.Calendar(firstweekday=0).monthdatescalendar(first.year, first.month)
    data = list_sessions(weeks[0][0], weeks[-1][-1], conn)
    by_day = defaultdict(list)
    for s in data["sessions"]:
        by_day[s["date"]].append(s)
    runs_by_day = defaultdict(list)
    for a in data["unplanned_runs"]:
        runs_by_day[a["date"]].append(a)

    rows = []
    for week in weeks:
        days = [(d, by_day[d.isoformat()], runs_by_day[d.isoformat()]) for d in week]
        planned = sum(s["distance_km"] for _, ss, _ in days for s in ss if s["status"] != "skipped")
        done = sum(s["actual_km"] or 0 for _, ss, _ in days for s in ss) + sum(
            a["distance_km"] or 0 for _, _, rs in days for a in rs)
        rows.append({"days": days, "planned_km": planned, "done_km": done})

    prev_month = (first - timedelta(days=1)).strftime("%Y-%m")
    next_month = (first + timedelta(days=32)).strftime("%Y-%m")
    proposal = db.row(conn, "SELECT id FROM plans WHERE status = 'proposed'")
    return page(request, "calendar.html", rows=rows, first=first, today=today(), prev_month=prev_month,
                next_month=next_month, active=_active_plan(conn), proposal=proposal)


@app.get("/stats")
def stats_page(request: Request, weeks: int = 12, conn=Depends(get_conn)):
    weeks = weeks if weeks in (4, 12, 26, 52) else 12
    return page(request, "stats.html", data=stats(conn, weeks), weeks=weeks)


@app.post("/sync")
def sync_page(request: Request, conn=Depends(get_conn)):
    sync(conn)
    return redirect(request.headers.get("referer") or "/")


# --- Plans ----------------------------------------------------------------------------

@app.get("/plan/new")
def new_plan_page(request: Request):
    return page(request, "new_plan.html", form={}, error=None)


@app.post("/plan/new")
def new_plan_submit(request: Request, race_name: str = Form(), race_distance: str = Form(),
                    race_date: date = Form(), target_time: str = Form(), current_5k: str = Form(),
                    weekly_km: float = Form(15), runs_per_week: int = Form(4), long_run_day: str = Form("saturday"),
                    conn=Depends(get_conn)):
    form = dict(race_name=race_name, race_distance=race_distance, race_date=race_date, target_time=target_time,
                current_5k=current_5k, weekly_km=weekly_km, runs_per_week=runs_per_week, long_run_day=long_run_day)
    try:
        plan_id = new_plan(NewPlan(**form), conn)["plan_id"]
    except (HTTPException, ValueError) as e:
        return page(request, "new_plan.html", form=form, error=getattr(e, "detail", "Check the times (e.g. 25:00 or 1:50:00)."))
    return redirect(f"/plan/{plan_id}")


@app.get("/plan/update")
def update_plan_page(request: Request, conn=Depends(get_conn)):
    active = _active_plan(conn)
    if not active:
        return redirect("/")
    return page(request, "update_plan.html", plan=active, error=None)


@app.post("/plan/update")
def update_plan_submit(request: Request, adjust: str = Form("pace"), feel: str | None = Form(None),
                       new_5k: str | None = Form(None), conn=Depends(get_conn)):
    try:
        body = PlanUpdate(adjust=adjust, feel=feel or None, new_5k=new_5k or None)
        plan_id = update_plan(body, conn)["plan_id"]
    except (HTTPException, ValueError) as e:
        error = getattr(e, "detail", "Check the 5k time (e.g. 23:30).")
        return page(request, "update_plan.html", plan=_active_plan(conn), error=error)
    return redirect(f"/plan/{plan_id}")


@app.get("/plan/{plan_id}")
def plan_page(request: Request, plan_id: int, conn=Depends(get_conn)):
    summary = plan_summary(conn, plan_id)
    long_day = summary["plan"]["long_run_day"]
    weeks = defaultdict(list)  # keyed by the week's long-run date
    for s in summary["sessions"]:
        d = date.fromisoformat(s["date"])
        weeks[d + timedelta(days=(long_day - d.weekday()) % 7)].append(s)
    return page(request, "plan.html", s=summary, plan=summary["plan"], weeks=sorted(weeks.items()))


@app.post("/plan/{plan_id}/approve")
def approve_page(plan_id: int, conn=Depends(get_conn)):
    approve_plan(plan_id, conn)
    return redirect("/")


@app.post("/plan/{plan_id}/delete")
def delete_page(plan_id: int, conn=Depends(get_conn)):
    delete_plan(plan_id, conn)
    return redirect("/")


# --- Sessions -------------------------------------------------------------------------

@app.get("/session/{session_id}")
def session_page(request: Request, session_id: int, conn=Depends(get_conn)):
    s = db.row(conn, """SELECT s.*, p.vdot, p.status AS plan_status FROM sessions s
                        JOIN plans p ON p.id = s.plan_id WHERE s.id = ?""", session_id)
    if not s:
        raise HTTPException(404, "Session not found.")
    run = db.row(conn, "SELECT * FROM activities WHERE id = ?", s["activity_id"]) if s["activity_id"] else None
    verdict = None
    if run and run["distance_km"] and s["kind"] in ("easy", "long", "strides"):
        pace = run["moving_time_s"] / run["distance_km"]
        fast, slow = paces(s["vdot"])["E"]
        # Easy running only goes wrong by being too fast; a little slower than the range is fine.
        verdict = "too fast for an easy run" if pace < fast - 5 else "on target" if pace <= slow + 20 else "slow, but fine"
    return page(request, "session.html", s=s, run=run, verdict=verdict,
                editable=s["plan_status"] == "active" and s["status"] == "planned")


@app.post("/session/{session_id}/move")
def move_page(session_id: int, new_date: date = Form(), conn=Depends(get_conn)):
    move_session(session_id, Move(date=new_date), conn)
    return redirect(f"/session/{session_id}")


@app.post("/session/{session_id}/skip")
def skip_page(session_id: int, conn=Depends(get_conn)):
    skip_session(session_id, conn)
    return redirect(f"/session/{session_id}")
