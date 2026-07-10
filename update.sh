#!/usr/bin/env bash
#
# Self-update for a facility instance: pull the latest code and, only if the
# commit actually changed, reinstall dependencies, apply migrations, refresh
# static files, and restart the service.
#
# Safe to run repeatedly (idempotent) and safe to run from cron — when nothing
# has changed it does almost no work. Designed to be run as root (the service
# restart needs systemctl).
#
#   sudo bash /opt/upload-appointments/update.sh
#
set -euo pipefail

APP_DIR="/opt/upload-appointments"
VENV_DIR="$APP_DIR/venv"
SERVICE_NAME="upload-appointments"
SERVICE_USER="www-data"
LOCK_FILE="/var/lock/upload-appointments-update.lock"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# --- Only one update at a time (cron + a manual run must not overlap) ---
exec 200>"$LOCK_FILE"
if ! flock -n 200; then
    log "Another update is already running; skipping."
    exit 0
fi

# git runs here as root while the tree is owned by $SERVICE_USER; mark it safe
# so git doesn't refuse with "dubious ownership".
git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true

cd "$APP_DIR"

BEFORE=$(git rev-parse HEAD)
log "Current revision: $BEFORE"

# Force the tree to exactly match origin/main, discarding any local drift
# (hand-edits to tracked files on the facility box). Runtime files (.env,
# db.sqlite3, secrets.env) are gitignored, so reset --hard leaves them
# untouched.
if ! git fetch origin main; then
    log "ERROR: git fetch failed (no network?). Leaving instance on $BEFORE."
    exit 1
fi
git reset --hard origin/main

AFTER=$(git rev-parse HEAD)

if [[ "$BEFORE" == "$AFTER" ]]; then
    log "Already up to date; nothing to do."
    exit 0
fi

log "Updated $BEFORE -> $AFTER. Applying changes..."

# Dependencies may have changed.
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q

# Apply any new migrations (no-op when there are none).
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" migrate --no-input

# Refresh static files for the web UI.
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" collectstatic --noinput -v 0

# git/pip/collectstatic ran as root; hand ownership back to the service user.
# The glob also covers the WAL sidecar files (db.sqlite3-wal, -shm), which the
# web process and the upload process both need to write.
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
chmod 666 "$APP_DIR"/db.sqlite3* 2>/dev/null || true

# Restart so the long-running Gunicorn process picks up the new code. (The
# upload_appointments cron command is a fresh process and picks it up on its
# own, but the web UI needs the restart.)
systemctl restart "$SERVICE_NAME"

log "Update complete; service restarted on revision $AFTER."
