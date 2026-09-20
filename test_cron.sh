#!/usr/bin/env bash
#
# Tests for cron.sh, the crontab entry both deploy.sh and update.sh install.
#
# `crontab` is stubbed with a plain file and the --due-only support probe with a
# flag file, so this runs anywhere bash does and touches nothing real.
#
#   bash test_cron.sh
#
# Worth having because this is the one piece that reaches into a facility box and
# rewrites something it did not create. Wiping an instance's other cron entries,
# or installing a line the code on disk cannot run, would both be silent until
# somebody noticed the uploads had stopped.
set -uo pipefail

REPO="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

export APP_DIR="/opt/upload-appointments"
export VENV_DIR="$APP_DIR/venv"
export SERVICE_USER="www-data"

CRONTAB_FILE="$WORK/crontab"
SUPPORTED_FLAG="$WORK/supported"
: > "$CRONTAB_FILE"

# --- stubs -----------------------------------------------------------------
mkdir -p "$WORK/bin"
cat > "$WORK/bin/crontab" <<EOF
#!/usr/bin/env bash
if [[ "\${1:-}" == "-l" ]]; then
    [[ -s "$CRONTAB_FILE" ]] || exit 1
    cat "$CRONTAB_FILE"
elif [[ "\${1:-}" == "-" ]]; then
    cat > "$CRONTAB_FILE"
fi
EOF
chmod +x "$WORK/bin/crontab"
export PATH="$WORK/bin:$PATH"

. "$REPO/cron.sh"

# Override the probe: the real one shells out to manage.py.
cron_supported() { [[ -f "$SUPPORTED_FLAG" ]]; }

PASS=0; FAIL=0
check() {
    local name="$1" expected="$2" actual="$3"
    if [[ "$expected" == "$actual" ]]; then
        PASS=$((PASS + 1)); echo "  ok   $name"
    else
        FAIL=$((FAIL + 1))
        echo "  FAIL $name"
        echo "       expected: $expected"
        echo "       actual:   $actual"
    fi
}
set_crontab() { printf '%s\n' "$@" > "$CRONTAB_FILE"; }
schedule_of() { cron_current | head -n1 | awk '{print $1, $2, $3, $4, $5}'; }

echo "cron.sh"

# --- a box that has never had an entry -------------------------------------
: > "$CRONTAB_FILE"; touch "$SUPPORTED_FLAG"
reconcile_cron > /dev/null
check "installs an entry on a fresh box" 1 "$(cron_current | wc -l)"
check "runs twice an hour" "yes" \
    "$(schedule_of | grep -Eq '^[0-9]+,[0-9]+ \* \* \* \*$' && echo yes || echo no)"
check "uses --due-only" "yes" \
    "$(cron_current | grep -q -- '--due-only' && echo yes || echo no)"
check "still self-updates first" "yes" \
    "$(cron_current | grep -q 'update.sh' && echo yes || echo no)"
check "the two ticks are 30 minutes apart" "30" \
    "$(schedule_of | awk -F'[ ,]' '{print $2 - $1}')"

# --- idempotency ------------------------------------------------------------
BEFORE="$(cat "$CRONTAB_FILE")"
reconcile_cron > /dev/null
reconcile_cron > /dev/null
check "is idempotent" "$BEFORE" "$(cat "$CRONTAB_FILE")"

# --- migrating a box off the old nightly entry ------------------------------
set_crontab "37 23 * * * cd /opt/upload-appointments && bash /opt/upload-appointments/update.sh >> /var/log/u.log 2>&1; su -s /bin/sh -c '/opt/upload-appointments/venv/bin/python /opt/upload-appointments/manage.py upload_appointments' www-data >> /var/log/up.log 2>&1"
reconcile_cron > /dev/null
check "migrates the old nightly entry" "7,37 * * * *" "$(schedule_of)"
check "leaves exactly one entry" 1 "$(cron_current | wc -l)"

# 37 % 30 = 7, so a minute past the half hour folds back into the first half.
set_crontab "45 2 * * * ... upload_appointments ..."
reconcile_cron > /dev/null
check "folds a minute over 30 back into range" "15,45 * * * *" "$(schedule_of)"

# --- the offset is a facility's own, and survives ---------------------------
set_crontab "7,37 * * * * cd /opt ... upload_appointments --due-only ..."
reconcile_cron > /dev/null
check "keeps the minute the box already uploads on" "7,37 * * * *" "$(schedule_of)"

# --- code that does not understand the flag ---------------------------------
rm -f "$SUPPORTED_FLAG"
OLD="30 23 * * * ... upload_appointments ..."
set_crontab "$OLD"
reconcile_cron > /dev/null
check "leaves the entry alone when the code is too old" "$OLD" "$(cat "$CRONTAB_FILE")"
touch "$SUPPORTED_FLAG"

# --- unrelated entries are not disturbed ------------------------------------
set_crontab "0 4 * * * /usr/bin/certbot renew" "@reboot /usr/local/bin/something"
reconcile_cron > /dev/null
check "keeps other crontab entries" "yes" \
    "$(grep -q certbot "$CRONTAB_FILE" && grep -q something "$CRONTAB_FILE" && echo yes || echo no)"
check "adds itself alongside them" 3 "$(wc -l < "$CRONTAB_FILE")"

# --- a box that somehow accumulated two entries -----------------------------
set_crontab "5 1 * * * ... upload_appointments ..." "9 2 * * * ... upload_appointments ..."
reconcile_cron > /dev/null
check "collapses duplicate entries" 1 "$(cron_current | wc -l)"

echo
echo "$PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
