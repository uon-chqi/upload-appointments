#!/usr/bin/env bash
#
# The crontab entry, defined once for both the installer (deploy.sh) and the
# self-update that runs afterwards (update.sh).
#
# It lives here, in the repo, so that changing the schedule is a push rather than
# a visit to every facility: update.sh pulls this file and then reconciles the
# box's crontab against it. That is the only way a schedule change reaches an
# instance nobody logs into.
#
# Sourced, never executed. Expects APP_DIR, VENV_DIR and SERVICE_USER to be set.

UPLOAD_LOG="/var/log/upload-appointments.log"
UPDATE_LOG="/var/log/upload-appointments-update.log"

# Twice an hour, day and night, rather than one slot overnight. A facility that
# switches its machine off in the evening, or whose internet appears somewhere in
# the middle of the afternoon, is uploaded by whichever tick happens to find the
# machine on and the link up — and a nightly slot, for those facilities, is a
# slot that never comes round at all.
#
# The two ticks are offset by a per-facility number of minutes rather than landing
# on :00 and :30. With ~250 instances reporting to one central API, a fixed
# half-hour boundary would be a worse thundering herd than the nightly slot it
# replaces; the offset spreads them across the hour. It is derived from whatever
# the box is already running (see cron_offset), so a facility keeps its slot
# across updates and re-deploys.
#
# `upload_appointments --due-only` decides for itself whether there is anything
# to do: it uploads each facility for the period it actually missed, and when
# nothing is behind — the other forty-seven ticks of the day — it costs a few
# seconds and writes a line to the log.
cron_schedule() {
    local offset="$1"
    echo "$offset,$((offset + 30)) * * * *"
}

# Self-update first, then the upload, joined with ';' not '&&': a failed pull
# (no network, most likely) must not cost the facility its upload. Both streams
# go to log files, so cron has nothing to mail.
#
# The upload runs as $SERVICE_USER, not root. In WAL mode SQLite keeps
# db.sqlite3-wal and -shm alongside the database, and whichever user creates them
# owns them — a root-owned WAL would lock the www-data web process out of its own
# database. update.sh still needs root for systemctl.
cron_line() {
    local offset="$1"
    local upload="$VENV_DIR/bin/python $APP_DIR/manage.py upload_appointments --due-only"
    echo "$(cron_schedule "$offset") cd $APP_DIR && bash $APP_DIR/update.sh >> $UPDATE_LOG 2>&1; su -s /bin/sh -c '$upload' $SERVICE_USER >> $UPLOAD_LOG 2>&1"
}

cron_current() {
    crontab -l 2>/dev/null | grep -F 'upload_appointments' || true
}

# The minute of the hour this facility uploads on, preserved across updates.
#
# Reads it back out of whatever entry is installed: "7,37 * * * *" gives 7, and
# the older nightly form "7 23 * * *" gives 7 as well, so a box migrating from a
# nightly slot keeps the minute it was already spread onto. Only a box with no
# entry at all draws a new one.
cron_offset() {
    local field
    field="$(cron_current | head -n1 | awk '{print $1}')"
    field="${field%%,*}"
    if [[ "$field" =~ ^[0-9]+$ ]]; then
        echo $((field % 30))
    else
        echo $((RANDOM % 30))
    fi
}

# Whether the code currently on disk understands the flag the entry uses. An
# instance part-way through an update, or rolled back to an older revision, must
# not be handed a crontab line that fails on every tick — better to leave the old
# entry in place and let the next successful update promote it.
cron_supported() {
    "$VENV_DIR/bin/python" "$APP_DIR/manage.py" upload_appointments --help 2>/dev/null \
        | grep -q -- '--due-only'
}

# Bring the crontab in line with this revision. Idempotent, and a no-op costing
# nothing once the entry already matches — which is the case on all but the one
# tick that migrates a box, so it is safe to call on every run.
#
# Returns 0 whether or not it changed anything: a crontab that cannot be updated
# is not a reason to abandon an upload.
reconcile_cron() {
    local offset desired current others new
    offset="$(cron_offset)"
    desired="$(cron_line "$offset")"
    current="$(cron_current)"

    if [[ "$current" == "$desired" ]]; then
        return 0
    fi

    if ! cron_supported; then
        return 0
    fi

    # Everything is read before anything is written. Piping `crontab -l` straight
    # into `crontab -` reads and replaces the same table concurrently, which is
    # a race worth not having when losing it means wiping the box's other cron
    # entries.
    #
    # Dropping every existing entry for this app before adding ours also means a
    # box that somehow accumulated two does not end up uploading twice a tick.
    others="$(crontab -l 2>/dev/null | grep -vF 'upload_appointments' || true)"
    if [[ -n "$others" ]]; then
        printf -v new '%s\n%s' "$others" "$desired"
    else
        new="$desired"
    fi

    if printf '%s\n' "$new" | crontab -; then
        if [[ -n "$current" ]]; then
            echo "Cron entry updated to: $(cron_schedule "$offset") (--due-only)"
        else
            echo "Cron entry installed: $(cron_schedule "$offset") (--due-only)"
        fi
    else
        echo "WARNING: could not write the crontab; leaving the existing entry."
    fi
    return 0
}
