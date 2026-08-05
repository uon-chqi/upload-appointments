# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django 4.2 LTS project for uploading appointments (pinned for Python 3.8 / Ubuntu 20.04 compatibility). Uses SQLite as the default database.

It reads appointment data from one or more KenyaEMR/OpenMRS MySQL databases and uploads it to the Ushauri DIFF platform, on a nightly cron schedule or on demand from a web dashboard.

Two modes, chosen by the `AppSettings.multi_facility_enabled` flag (single row, toggled at `/multi-facilities`):

- **Single facility** (default): connects to the OpenMRS database configured via `OPENMRS_DB_*` environment variables.
- **Multi facility**: connects to every active `Facility` row, each a separate KenyaEMR container with its own MySQL host, port, database name, and credentials. Built for a client running ~100 containers behind one dashboard.

The flag only controls what the nightly cron job uploads. Manual uploads work in both modes: `/` uploads the environment-configured facility, `/multi-facilities` uploads all active facilities.

## Project Structure

- `uploadappointments/` — Django project config (settings, root URLs, WSGI/ASGI)
- `upload/` — Main Django app for appointment upload functionality
  - `openmrs.py` — Per-facility MySQL access: `FacilityConfig`, `connect()`, `fetch_appointments()`, `probe()`, and the appointment SQL
  - `services.py` — `TokenProvider`, batch upload with retry, `create_run()` / `execute_run()`, stale-run detection, `spawn_run()`
  - `crypto.py` — Fernet encryption for facility MySQL passwords at rest
- `manage.py` — Django management entry point

## Architecture Notes

- **OpenMRS is not a Django database alias.** Facilities are separate containers, so `upload/openmrs.py` opens a raw `MySQLdb` connection per facility. The appointment SQL uses unqualified table names; the connected database supplies the schema. Do not reintroduce an `openmrs` entry in `DATABASES`.
- **The first run after install is a backfill.** A fresh deployment has nothing
  upstream, so instead of one night's window it uploads every pending appointment
  (`AppointmentQuery` minus the `date_appointment_scheduled` filter). Whichever
  comes first — the operator pressing "Upload all pending now" or the next cron
  firing — flips `AppSettings.initial_backfill_done`, and it never happens again
  unless forced with `--backfill`. Nightly runs filter on
  `date_appointment_scheduled`, i.e. when the appointment was booked, not when it
  falls; an explicitly requested date range always suppresses the automatic
  backfill.
- **A run is the unit of work.** `UploadRun` has one `UploadLog` per target facility, created up front. Single-facility runs have exactly one log with `facility = NULL`, which is also how logs predating multi-facility support read. Run status is `success` / `partial` / `failed` — with 100 facilities, "a few failed" is the normal case, hence `partial` and the retry-failed action.
- **Uploads run in a detached subprocess**, not a thread inside gunicorn: a 100-facility run takes tens of minutes and must survive a worker restart. The web view creates the run, then spawns `manage.py upload_appointments --run-id=N`. Cron uses the same code path. A run whose heartbeat goes cold for `UPLOAD_STALE_MINUTES` is marked failed by `mark_stale_runs()`.
- **Facilities upload concurrently** via a `ThreadPoolExecutor` sized by `MULTI_FACILITY_WORKERS`. The central API is the only shared bottleneck; each facility has its own MySQL.
- **One API token per run**, shared by all workers, re-authenticated once on a 401 (`TokenProvider`). A long run can outlive its token.
- **The DIFF platform identifies facilities by MFL code**, read from the OpenMRS data (`kenyaemr.defaultLocation` → `location_attribute`), never from `Facility.name`. Two containers sharing an MFL would overwrite each other upstream, so `mfl_code` is unique and the test-connection probe blocks a collision at setup time.
- **SQLite runs in WAL mode** (`upload/apps.py`) so upload workers can write progress while the web process reads. The WAL sidecar files are shared between processes, so cron runs the upload as the service user, not root.

## Known Limitations

- `systemctl restart` kills a web-triggered upload in flight, because the detached child stays in the service's cgroup. Stale-run detection catches it within `UPLOAD_STALE_MINUTES`. The nightly cron upload is unaffected — it runs outside the service cgroup.
- Only one run may be in flight at a time, deployment-wide. Cron refuses to start when one is active.

## Common Commands

```bash
# Run development server
python manage.py runserver

# Run all tests
python manage.py test

# Run tests for a specific app
python manage.py test upload

# Run a single test case
python manage.py test upload.tests.TestClassName

# Apply migrations
python manage.py migrate

# Create new migrations after model changes
python manage.py makemigrations

# Create a superuser for admin access
python manage.py createsuperuser
```

## Key Configuration

- Settings module: `uploadappointments.settings`
- Root URL conf: `uploadappointments.urls`
- Database: SQLite (`db.sqlite3`, WAL mode) for Django models; MySQL for OpenMRS (read-only, opened per facility)
- The `upload` app is registered in INSTALLED_APPS
- Single-facility OpenMRS connection via environment variables (`OPENMRS_DB_*`)
- Ushauri DIFF platform credentials via environment variables (`CHQI_API_*`)
- `/multi-facilities` and all facility CRUD are staff-only

## Environment Variables

```bash
# Single-facility OpenMRS connection (ignored in multi-facility mode)
OPENMRS_DB_NAME=
OPENMRS_DB_USER=
OPENMRS_DB_PASSWORD=
OPENMRS_DB_HOST=
OPENMRS_DB_PORT=3306
OPENMRS_DB_LABEL='Local facility'   # display name in upload history

# Ushauri DIFF platform (one account for all facilities)
CHQI_API_BASE_URL=
CHQI_API_USERNAME=
CHQI_API_PASSWORD=

# Django
DJANGO_SECRET_KEY=
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=*

# Encrypts facility MySQL passwords at rest. Falls back to SECRET_KEY if unset.
# Rotating whichever is in use means re-entering every facility password.
FIELD_ENCRYPTION_KEY=

# Upload tuning
UPLOAD_BATCH_SIZE=10
MULTI_FACILITY_WORKERS=5     # concurrent facilities; the central API is the bottleneck
UPLOAD_STALE_MINUTES=20      # a run with no progress for this long is marked failed
```

## Cron Job

One entry regardless of mode — the command reads `AppSettings.multi_facility_enabled`
and uploads either the environment-configured facility or every active one.
Run it as the service user, not root, so the SQLite WAL files stay writable by
the web process.

```bash
# Daily upload at 6:00 AM (yesterday to today)
0 6 * * * su -s /bin/sh -c '/path/to/venv/bin/python /path/to/manage.py upload_appointments' www-data
```

Exit codes: `0` on success or partial success (some facilities failed — expected
at scale, retryable from the UI), `1` when every facility failed or a run is
already in progress.

```bash
# Re-run a single facility that failed
python manage.py upload_appointments --facility 7 --date-from 2026-01-01 --date-to 2026-01-02

# Override concurrency for one run
python manage.py upload_appointments --workers 2

# Force another full upload of every pending appointment. The first cron job
# after install does this by itself; this is for repeating it deliberately.
# Refuses to combine with a date range.
python manage.py upload_appointments --backfill
```

A backfill run stores `date_from`/`date_to` as the day it ran and sets
`is_backfill`; the UI shows "All pending" for those rather than a date range.
Facilities that succeed get `Facility.initial_backfill_at` stamped, so the
"Initial Load" column shows exactly which containers still need one. The
deployment-wide flag is set even when a backfill ends `partial` — at a hundred
facilities a few are always unreachable, and repeating a full-history upload for
the rest every night to chase them would be worse than leaving them to the
"Retry failed" button, which re-runs the backfill for just those facilities.
