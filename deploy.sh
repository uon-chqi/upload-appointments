#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/upload-appointments"
VENV_DIR="$APP_DIR/venv"
REPO_URL="https://github.com/uon-chqi/upload-appointments.git"
CRON_SCHEDULE="0 23 * * *"

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
echo "[1/7] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-dev gcc pkg-config \
    libmysqlclient-dev git > /dev/null
echo "  Done."

# --- Clone or pull the repo ---
echo "[2/7] Setting up application code..."
if [[ -d "$APP_DIR/.git" ]]; then
    echo "  Repository exists. Pulling latest changes..."
    git -C "$APP_DIR" pull --ff-only
else
    echo "  Cloning repository..."
    git clone "$REPO_URL" "$APP_DIR"
fi
echo "  Done."

# --- Create virtual environment and install dependencies ---
echo "[3/7] Setting up Python virtual environment..."
python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip -q
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt" -q
echo "  Done."

# --- Collect .env configuration ---
ENV_FILE="$APP_DIR/.env"
if [[ -f "$ENV_FILE" ]]; then
    echo "[4/7] Existing .env file found at $ENV_FILE"
    read -rp "  Overwrite it? (y/N): " overwrite
    if [[ "$overwrite" != "y" && "$overwrite" != "Y" ]]; then
        echo "  Keeping existing .env file."
    else
        configure_env=true
    fi
else
    configure_env=true
fi

if [[ "${configure_env:-false}" == "true" ]]; then
    echo "[4/7] Configuring environment variables..."
    echo

    read -rp "  OpenMRS DB Host [127.0.0.1]: " db_host
    db_host=${db_host:-127.0.0.1}

    read -rp "  OpenMRS DB Port [3306]: " db_port
    db_port=${db_port:-3306}

    read -rp "  OpenMRS DB Name [openmrs]: " db_name
    db_name=${db_name:-openmrs}

    read -rp "  OpenMRS DB User [root]: " db_user
    db_user=${db_user:-root}

    read -rsp "  OpenMRS DB Password: " db_password
    echo

    echo

    read -rp "  CHQI API Base URL [https://api-sms-portal.chqi.org]: " api_url
    api_url=${api_url:-https://api-sms-portal.chqi.org}

    read -rp "  CHQI API Username: " api_user

    read -rsp "  CHQI API Password: " api_password
    echo

    cat > "$ENV_FILE" <<ENVEOF
# OpenMRS MySQL Database
OPENMRS_DB_NAME=$db_name
OPENMRS_DB_USER=$db_user
OPENMRS_DB_PASSWORD=$db_password
OPENMRS_DB_HOST=$db_host
OPENMRS_DB_PORT=$db_port

# CHQI API
CHQI_API_BASE_URL=$api_url
CHQI_API_USERNAME=$api_user
CHQI_API_PASSWORD=$api_password
ENVEOF

    chmod 600 "$ENV_FILE"
    echo "  .env saved."
fi

# --- Run migrations ---
echo "[5/7] Running database migrations..."
"$VENV_DIR/bin/python" "$APP_DIR/manage.py" migrate --no-input
echo "  Done."

# --- Create Django superuser if needed ---
echo "[6/7] Django admin user setup..."
SUPERUSER_EXISTS=$("$VENV_DIR/bin/python" "$APP_DIR/manage.py" shell -c \
    "from django.contrib.auth.models import User; print(User.objects.filter(is_superuser=True).exists())")

if [[ "$SUPERUSER_EXISTS" == "False" ]]; then
    echo "  No admin user found. Creating one now..."
    "$VENV_DIR/bin/python" "$APP_DIR/manage.py" createsuperuser
else
    echo "  Admin user already exists. Skipping."
fi

# --- Set up cron job ---
echo "[7/7] Setting up cron job (daily at 11:00 PM)..."
CRON_CMD="cd $APP_DIR && $VENV_DIR/bin/python manage.py upload_appointments >> /var/log/upload-appointments.log 2>&1"
CRON_LINE="$CRON_SCHEDULE $CRON_CMD"

# Remove any existing cron entry for this app, then add the new one
(crontab -l 2>/dev/null | grep -v "upload_appointments" || true; echo "$CRON_LINE") | crontab -
touch /var/log/upload-appointments.log
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
echo "  Cron:          Daily at 11:00 PM"
echo "  Log:           /var/log/upload-appointments.log"
echo
echo "  To run the dev server:"
echo "    cd $APP_DIR && $VENV_DIR/bin/python manage.py runserver 0.0.0.0:8000"
echo
echo "  To update later:"
echo "    sudo bash $APP_DIR/deploy.sh"
echo
