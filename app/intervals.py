"""The four intervals.icu calls the app needs. Athlete id 0 = the owner of the API key."""
import os

import httpx

BASE = "https://intervals.icu/api/v1/athlete/0"


def _client():
    return httpx.Client(
        auth=("API_KEY", os.environ["INTERVALS_API_KEY"]),
        # Cloudflare in front of intervals.icu can block default client user agents.
        headers={"User-Agent": "Mozilla/5.0 (frog-runna)"},
        timeout=30,
    )


def create_event(date, name, workout_text):
    with _client() as c:
        r = c.post(f"{BASE}/events", json={
            "category": "WORKOUT",
            "type": "Run",
            "start_date_local": f"{date}T00:00:00",
            "name": name,
            "description": workout_text,
        })
        r.raise_for_status()
        return r.json()["id"]


def move_event(event_id, date):
    with _client() as c:
        c.put(f"{BASE}/events/{event_id}", json={"start_date_local": f"{date}T00:00:00"}).raise_for_status()


def delete_event(event_id):
    with _client() as c:
        r = c.delete(f"{BASE}/events/{event_id}")
        if r.status_code != 404:  # already gone is fine
            r.raise_for_status()


def fetch_runs(oldest, newest):
    with _client() as c:
        r = c.get(f"{BASE}/activities", params={"oldest": str(oldest), "newest": str(newest)})
        r.raise_for_status()
        return [a for a in r.json() if a.get("type") in ("Run", "VirtualRun", "TrailRun")]
