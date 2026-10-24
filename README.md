# Frog Runna

Self-hosted running coach: Jack Daniels VDOT training plans, pushed to Garmin via intervals.icu.

## Setup
1. intervals.icu: Settings > Connections > Garmin: connect it and turn on "Upload planned workouts".
2. `cp .env.example .env` and add your intervals.icu API key (Settings > Developer Settings).
3. `docker compose up -d --build`, then open `http://<pi-ip>:8080`.

## Use
The web pages: a calendar (`/`), new plan, plan preview/approve, session (move/skip) and update plan.

## JSON API (try it from `/docs`)
- `POST /api/plans`: create a proposed plan. `GET /api/plans/{id}`: preview it.
- `POST /api/plans/{id}/approve`: make it active and push sessions to intervals.icu.
- `DELETE /api/plans/{id}`: delete a plan (proposed or active) and remove its workouts from intervals.icu. Runs are kept.
- `POST /api/plans/update`: `{"adjust": "pace", "feel": "easier"}` (VDOT +1; `much_easier` +2, `harder` -1,
  `much_harder` -2), `{"adjust": "distance", "feel": "easier"}` (weekly km +10%; +/-20% for the "much" options),
  or `{"new_5k": "23:30"}`. Creates a proposal to approve.
- `GET /api/sessions?start=&end=`, `POST /api/sessions/{id}/move` `{"date": ...}`, `POST /api/sessions/{id}/skip`.
- `POST /api/sync`: push pending sessions and pull runs (also runs every 30 min).

## Development
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/pytest
```
