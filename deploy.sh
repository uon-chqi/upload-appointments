#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/upload-appointments"
VENV_DIR="$APP_DIR/venv"
REPO_URL="https://github.com/uon-chqi/upload-appointments.git"
SERVICE_NAME="upload-appointments"
SERVICE_USER="www-data"

# --- Standard configuration (same for every install) ---
DB_HOST="127.0.0.1"
DB_PORT="3306"
DB_NAME="openmrs"
API_BASE_URL="https://ushauriplus-api.chqi.org"
SERVER_PORT="9162"
ADMIN_USERNAME="admin"
ADMIN_EMAIL="admin@nascop.org"
SECRETS_FILE_ENC="$APP_DIR/secrets.env.gpg"

# Helper: prompt the user for input (works even when script is piped)
prompt() {
    local var_name="$1" prompt_text="$2" default="${3:-}"
    local input
    echo -n "$prompt_text" > /dev/tty
    read -r input < /dev/tty
    input="${input:-$default}"
    eval "$var_name=\$input"
}

prompt_silent() {
    local var_name="$1" prompt_text="$2"
    local input
    echo -n "$prompt_text" > /dev/tty
    read -rs input < /dev/tty
    echo > /dev/tty
    eval "$var_name=\$input"
}

echo "=========================================="
echo "  Upload Appointments - Deployment Script"
echo "=========================================="
echo

# --- Must run as root ---
if [[ $EUID -ne 0 ]]; then
    echo "Error: Please run this script as root (sudo)."
    exit 1
fi

# --- Install system dependencies ---
echo "[1/9] Installing system dependencies..."
apt-get update -qq --allow-releaseinfo-change 2>/dev/null || true
apt-get install -y -qq python3 python3-venv python3-dev gcc pkg-config libmysqlclient-dev git gnupg > /dev/null
echo "  Done."

# --- Clone or pull the repo ---
echo "[2/9] Setting up application code..."
# The repo is chowned to $SERVICE_USER below, but git runs here as root.
# Mark it as a safe directory so git doesn't refuse with "dubious ownership".
git config --global --add safe.directory "$APP_DIR"
if [[ -d "$APP_DIR/.git" ]]; then
    echo "  Repository exists. Pulling latest changes..."
    git -C "$APP_DIR" pull --ff-only
else
    echo "  Cloning repository..."
    git clone "$REPO_URL" "$APP_DIR"
fi
echo "  Done."

# --- Create virtual environment and install dependencies ---
echo "[3/9] Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  Done."

# --- Load shared secrets (encrypted, committed as secrets.env.gpg) ---
echo "[4/9] Loading shared secrets and configuring environment..."

if [[ ! -f "$SECRETS_FILE_ENC" ]]; then
    echo "  Error: $SECRETS_FILE_ENC not found. Cannot continue."
    exit 1
fi

prompt gpg_passphrase "  Secrets passphrase: "

if ! secrets_plaintext=$(gpg --batch --quiet --pinentry-mode loopback \
        --passphrase "$gpg_passphrase" -d "$SECRETS_FILE_ENC" 2>/dev/null); then
    echo "  Error: could not decrypt secrets.env.gpg (wrong passphrase?)."
    exit 1
fi

# Loads CHQI_API_USERNAME, CHQI_API_PASSWORD and DJANGO_ADMIN_PASSWORD.
source /dev/stdin <<< "$secrets_plaintext"
unset secrets_plaintext gpg_passphrase
echo "  Shared secrets loaded."

# --- Collect .env configuration ---
ENV_FILE="$APP_DIR/.env"
configure_env=false

if [[ -f "$ENV_FILE" ]]; then
    echo "  Existing .env file found at $ENV_FILE"
    prompt overwrite "  Overwrite it? (y/N): "
    if [[ "$overwrite" == "y" || "$overwrite" == "Y" ]]; then
        configure_env=true
    else
        echo "  Keeping existing .env file."
    fi
else
    configure_env=true
fi

if [[ "$configure_env" == "true" ]]; then
    echo "  Configuring environment variables..."
    echo

    prompt db_user     "  OpenMRS DB User: "
    prompt db_password "  OpenMRS DB Password: "

    # These two must survive a re-install. FIELD_ENCRYPTION_KEY decrypts the
    # facility MySQL passwords stored in db.sqlite3, and DJANGO_SECRET_KEY backs
    # existing sessions — regenerating either would silently break a working
    # multi-facility install. Reuse whatever the old .env had.
    existing_key() { grep -s "^$1=" "$ENV_FILE" | head -n1 | cut -d= -f2- || true; }
    FIELD_ENCRYPTION_KEY="$(existing_key FIELD_ENCRYPTION_KEY)"
    FIELD_ENCRYPTION_KEY="${FIELD_ENCRYPTION_KEY:-$(openssl rand -hex 32)}"
    DJANGO_SECRET_KEY="$(existing_key DJANGO_SECRET_KEY)"
    DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-$(openssl rand -hex 32)}"

    cat > "$ENV_FILE" <<ENVEOF
OPENMRS_DB_NAME=${DB_NAME}
OPENMRS_DB_USER=${db_user}
OPENMRS_DB_PASSWORD=${db_password}
OPENMRS_DB_HOST=${DB_HOST}
OPENMRS_DB_PORT=${DB_PORT}
CHQI_API_BASE_URL=${API_BASE_URL}
CHQI_API_USERNAME=${CHQI_API_USERNAME}
CHQI_API_PASSWORD=${CHQI_API_PASSWORD}
DJANGO_SECRET_KEY=${DJANGO_SECRET_KEY}
FIELD_ENCRYPTION_KEY=${FIELD_ENCRYPTION_KEY}
DJANGO_DEBUG=False
DJANGO_ALLOWED_HOSTS=*
ENVEOF

    chmod 640 "$ENV_FILE"
    echo "  .env saved."
fi

# --- Run migrations ---
echo "[5/9] Running database migrations..."
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" migrate --no-input
echo "  Done."

# --- Collect static files ---
echo "[6/9] Collecting static files..."
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" collectstatic --noinput -v 0
echo "  Done."

# --- Create Django superuser if needed ---
echo "[7/9] Django admin user setup..."
SUPERUSER_EXISTS=$("$VENV_DIR/bin/python" "$APP_DIR/manage.py" shell -c \
    "from django.contrib.auth.models import User; print(User.objects.filter(is_superuser=True).exists())")

if [[ "$SUPERUSER_EXISTS" == "False" ]]; then
    echo "  No admin user found. Creating '$ADMIN_USERNAME'..."
    DJANGO_SUPERUSER_PASSWORD="$DJANGO_ADMIN_PASSWORD" "$VENV_DIR/bin/python" "$APP_DIR/manage.py" createsuperuser \
        --noinput --username "$ADMIN_USERNAME" --email "$ADMIN_EMAIL"
    echo "  Admin user '$ADMIN_USERNAME' created."
else
    echo "  Admin user already exists. Skipping."
fi

# --- Set file ownership ---
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
chmod 777 "$APP_DIR"
# The glob covers db.sqlite3-wal and -shm: in WAL mode those persist and are
# shared between the web process and the upload process, so both must write them.
chmod 666 "$APP_DIR"/db.sqlite3* 2>/dev/null || true

# --- Set up Gunicorn systemd service ---
echo "[8/9] Setting up Gunicorn service..."

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SVCEOF
[Unit]
Description=Upload Appointments (Gunicorn)
After=network.target

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/gunicorn uploadappointments.wsgi:application --bind 0.0.0.0:${SERVER_PORT} --workers 3 --timeout 120
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl restart "$SERVICE_NAME"
echo "  Gunicorn service started on port $SERVER_PORT."

# --- Set up cron job ---
# Each facility uploads once a day at a RANDOM time in the 6pm–6am off-hours
# window. With ~250 facilities all reporting to one central API, a fixed time
# (e.g. 11pm) would cause a thundering-herd spike; spreading the start times
# across the 12-hour idle window keeps peak concurrency low.
#
# The time is chosen ONCE at first install and preserved on later re-deploys,
# so updating the app does not reshuffle this facility to a new slot.
EXISTING_SCHEDULE=$(crontab -l 2>/dev/null | grep "upload_appointments" | head -n1 | awk '{print $1, $2, $3, $4, $5}' || true)

if [[ -n "$EXISTING_SCHEDULE" ]]; then
    CRON_SCHEDULE="$EXISTING_SCHEDULE"
    echo "[9/9] Keeping existing upload schedule ($CRON_SCHEDULE)..."
else
    # Off-hours window 6pm–6am wraps midnight, so list the hours explicitly.
    WINDOW_HOURS=(18 19 20 21 22 23 0 1 2 3 4 5)
    RAND_HOUR=${WINDOW_HOURS[$((RANDOM % ${#WINDOW_HOURS[@]}))]}
    RAND_MIN=$((RANDOM % 60))
    CRON_SCHEDULE="$RAND_MIN $RAND_HOUR * * *"
    echo "[9/9] Setting up cron job (random off-hours slot: $(printf '%02d:%02d' "$RAND_HOUR" "$RAND_MIN"))..."
fi

# Self-update first (best-effort: a failed pull must not stop the upload, so
# the two are joined with ';' not '&&'), then run the daily upload. Pulling
# immediately before uploading means each run uses the latest code — e.g. an
# updated SQL query — without any manual redeploy at the facility.
#
# The upload runs as $SERVICE_USER, not root. In WAL mode SQLite keeps
# db.sqlite3-wal and -shm alongside the database, and whichever user creates
# them owns them — a root-owned WAL would lock the www-data web process out of
# its own database. update.sh still needs root for systemctl.
UPDATE_LOG="/var/log/upload-appointments-update.log"
UPLOAD_CMD="$VENV_DIR/bin/python $APP_DIR/manage.py upload_appointments"
CRON_CMD="cd $APP_DIR && bash $APP_DIR/update.sh >> $UPDATE_LOG 2>&1; su -s /bin/sh -c '$UPLOAD_CMD' $SERVICE_USER >> /var/log/upload-appointments.log 2>&1"
CRON_LINE="$CRON_SCHEDULE $CRON_CMD"

# Remove any existing cron entry for this app, then add the new one
(crontab -l 2>/dev/null | grep -v "upload_appointments" || true; echo "$CRON_LINE") | crontab -
touch /var/log/upload-appointments.log "$UPDATE_LOG"
chmod 666 /var/log/upload-appointments.log "$UPDATE_LOG"
echo "  Cron job installed: $CRON_SCHEDULE"
echo "  Upload log: /var/log/upload-appointments.log"
echo "  Update log: $UPDATE_LOG"

echo
echo "=========================================="
echo "  Deployment complete!"
echo "=========================================="
echo
echo "  App location:  $APP_DIR"
echo "  Virtual env:   $VENV_DIR"
echo "  Config:        $ENV_FILE"
echo "  Service:       systemctl status $SERVICE_NAME"
echo "  Running on:    http://0.0.0.0:$SERVER_PORT"
echo "  Cron:          Daily at $CRON_SCHEDULE (random off-hours slot)"
echo "  Log:           /var/log/upload-appointments.log"
echo
echo "  Useful commands:"
echo "    sudo systemctl status $SERVICE_NAME    # Check service status"
echo "    sudo systemctl restart $SERVICE_NAME   # Restart service"
echo "    sudo systemctl stop $SERVICE_NAME      # Stop service"
echo "    sudo journalctl -u $SERVICE_NAME -f    # View live logs"
echo
echo "  To update later:"
echo "    sudo bash $APP_DIR/deploy.sh"
echo
