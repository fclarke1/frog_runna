import os
import sqlite3

DB_PATH = os.environ.get("DB_PATH", "data/runna.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,              -- proposed, active, archived
    replaces_plan_id INTEGER,          -- set on proposals that update an active plan
    race_name TEXT NOT NULL,
    race_km REAL NOT NULL,
    race_date TEXT NOT NULL,
    target_time_s INTEGER NOT NULL,
    vdot REAL NOT NULL,
    runs_per_week INTEGER NOT NULL,
    long_run_day INTEGER NOT NULL,     -- 0 = Monday
    start_date TEXT NOT NULL,
    start_weekly_km REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    plan_id INTEGER NOT NULL REFERENCES plans(id),
    date TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    distance_km REAL NOT NULL,
    description TEXT NOT NULL,
    workout_text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',  -- planned, done, skipped
    intervals_event_id INTEGER,
    activity_id TEXT
);
CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,               -- intervals.icu activity id
    date TEXT NOT NULL,
    name TEXT,
    distance_km REAL,
    moving_time_s INTEGER,
    avg_hr REAL,
    session_id INTEGER REFERENCES sessions(id)
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    # FastAPI may run a dependency and its endpoint on different threadpool threads.
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


def row(conn, sql, *args):
    r = conn.execute(sql, args).fetchone()
    return dict(r) if r else None
