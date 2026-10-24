"""Check the intervals.icu connection: list recent runs and push one test workout to tomorrow.

Run:  set -a; . ./.env; set +a; .venv/bin/python -m scripts.check_intervals
Then check the workout reaches Garmin Connect and the watch with pace targets, and delete it with:
      .venv/bin/python -m scripts.check_intervals --delete <event id>
"""
import sys
from datetime import date, timedelta

from app import intervals
from app.planner import _intervals
from app.vdot import paces

if len(sys.argv) == 3 and sys.argv[1] == "--delete":
    intervals.delete_event(int(sys.argv[2]))
    print("Deleted.")
    sys.exit()

runs = intervals.fetch_runs(date.today() - timedelta(days=14), date.today())
print(f"{len(runs)} runs in the last 14 days")
for r in runs[:5]:
    print(" ", r.get("start_date_local"), r.get("name"), r.get("distance"), "m", r.get("moving_time"), "s",
          "HR", r.get("average_heartrate"))

tomorrow = date.today() + timedelta(days=1)
s = _intervals(tomorrow, 25, paces(42))
event_id = intervals.create_event(tomorrow.isoformat(), "TEST " + s.title, s.workout_text)
print(f"\nCreated event {event_id} on {tomorrow}:\n{s.workout_text}")
