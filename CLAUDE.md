# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Django 5.2 project for uploading appointments. Uses SQLite as the default database. The project is in early stages with scaffolded boilerplate.

## Project Structure

- `uploadappointments/` — Django project config (settings, root URLs, WSGI/ASGI)
- `upload/` — Main Django app for appointment upload functionality
- `manage.py` — Django management entry point

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
- Database: SQLite (`db.sqlite3`) for Django models, MySQL for OpenMRS (read-only)
- The `upload` app is registered in INSTALLED_APPS
- OpenMRS MySQL connection configured via environment variables (`OPENMRS_DB_*`)
- CHQI API credentials configured via environment variables (`CHQI_API_*`)

## Environment Variables

```bash
OPENMRS_DB_NAME=openmrs
OPENMRS_DB_USER=root
OPENMRS_DB_PASSWORD=
OPENMRS_DB_HOST=127.0.0.1
OPENMRS_DB_PORT=3306
CHQI_API_BASE_URL=https://api-sms-portal.chqi.org
CHQI_API_USERNAME=superadmin
CHQI_API_PASSWORD=CHQIAdmin@2026
```

## Cron Job

```bash
# Daily upload at 6:00 AM (yesterday to today)
0 6 * * * cd /path/to/upload-appointments && python3 manage.py upload_appointments
```
