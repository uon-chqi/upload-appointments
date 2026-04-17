#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/upload-appointments"
VENV_DIR="$APP_DIR/venv"
REPO_URL="https://github.com/uon-chqi/upload-appointments.git"
CRON_SCHEDULE="0 23 * * *"
SERVICE_NAME="upload-appointments"
SERVICE_USER="www-data"

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
apt-get install -y -qq python3 python3-venv python3-dev gcc pkg-config libmysqlclient-dev git > /dev/null
echo "  Done."

# --- Clone or pull the repo ---
echo "[2/9] Setting up application code..."
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

# --- Collect .env configuration ---
ENV_FILE="$APP_DIR/.env"
configure_env=false

if [[ -f "$ENV_FILE" ]]; then
    echo "[4/9] Existing .env file found at $ENV_FILE"
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
    echo "[4/9] Configuring environment variables..."
    echo

    prompt db_host "  OpenMRS DB Host [127.0.0.1]: " "127.0.0.1"
    prompt db_port "  OpenMRS DB Port [3306]: " "3306"
    prompt db_name "  OpenMRS DB Name [openmrs]: " "openmrs"
    prompt db_user "  OpenMRS DB User [root]: " "root"
    prompt db_password "  OpenMRS DB Password: "
    prompt api_url "  CHQI API Base URL [https://api-sms-portal.chqi.org]: " "https://api-sms-portal.chqi.org"
    prompt api_user "  CHQI API Username: "
    prompt api_password "  CHQI API Password: "

    cat > "$ENV_FILE" <<ENVEOF
OPENMRS_DB_NAME=${db_name}
OPENMRS_DB_USER=${db_user}
OPENMRS_DB_PASSWORD=${db_password}
OPENMRS_DB_HOST=${db_host}
OPENMRS_DB_PORT=${db_port}
CHQI_API_BASE_URL=${api_url}
CHQI_API_USERNAME=${api_user}
CHQI_API_PASSWORD=${api_password}
ENVEOF

    chmod 644 "$ENV_FILE"
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
    echo "  No admin user found. Creating one now..."
    echo

    prompt admin_user "  Admin username: "
    prompt admin_email "  Admin email: "
    prompt admin_pass "  Admin password: "
    prompt admin_pass2 "  Confirm password: "

    if [[ "$admin_pass" != "$admin_pass2" ]]; then
        echo "  Error: Passwords do not match. Skipping superuser creation."
        echo "  You can create one later with: $VENV_DIR/bin/python $APP_DIR/manage.py createsuperuser"
    else
        DJANGO_SUPERUSER_PASSWORD="$admin_pass" "$VENV_DIR/bin/python" "$APP_DIR/manage.py" createsuperuser \
            --noinput --username "$admin_user" --email "$admin_email"
        echo "  Admin user '$admin_user' created."
    fi
else
    echo "  Admin user already exists. Skipping."
fi

# --- Set file ownership ---
chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"
chmod 777 "$APP_DIR"
chmod 666 "$APP_DIR/db.sqlite3"

# --- Set up Gunicorn systemd service ---
echo "[8/9] Setting up Gunicorn service..."

prompt server_port "  Server port [8000]: " "8000"

cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<SVCEOF
[Unit]
Description=Upload Appointments (Gunicorn)
After=network.target

[Service]
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/gunicorn uploadappointments.wsgi:application --bind 0.0.0.0:${server_port} --workers 3 --timeout 120
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" --quiet
systemctl restart "$SERVICE_NAME"
echo "  Gunicorn service started on port $server_port."

# --- Set up cron job ---
echo "[9/9] Setting up cron job (daily at 11:00 PM)..."
CRON_CMD="cd $APP_DIR && $VENV_DIR/bin/python manage.py upload_appointments >> /var/log/upload-appointments.log 2>&1"
CRON_LINE="$CRON_SCHEDULE $CRON_CMD"

# Remove any existing cron entry for this app, then add the new one
(crontab -l 2>/dev/null | grep -v "upload_appointments" || true; echo "$CRON_LINE") | crontab -
touch /var/log/upload-appointments.log
chmod 666 /var/log/upload-appointments.log
echo "  Cron job installed: $CRON_SCHEDULE"
echo "  Log file: /var/log/upload-appointments.log"

echo
echo "=========================================="
echo "  Deployment complete!"
echo "=========================================="
echo
echo "  App location:  $APP_DIR"
echo "  Virtual env:   $VENV_DIR"
echo "  Config:        $ENV_FILE"
echo "  Service:       systemctl status $SERVICE_NAME"
echo "  Running on:    http://0.0.0.0:$server_port"
echo "  Cron:          Daily at 11:00 PM"
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
