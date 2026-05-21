# Appointments Upload Service — Installation Guide

The Appointments Upload Service reads appointment data from OpenMRS/KenyaEMR 
database and uploads it to the Ushauri DIFF platform. It runs as a
background service with a web dashboard for monitoring, and performs an
automatic upload once every day.

## Prerequisites

Before you begin, make sure you have:

- An Ubuntu server (20.04 LTS or newer) with `sudo` / root access.
- A working internet connection — the installer downloads system packages,
  the application code, and Python dependencies.
- A running KenyaEMR instance with its MySQL database.
- OpenMRS database credentials — a MySQL user with **read access** to the
  OpenMRS database.
- The **secrets passphrase** for this deployment. It is shared separately by
  the NASCOP team.

## 1. Remove any previous installation (re-install only)

Skip this step on a first-time install. If you are re-installing, remove the
old files first:

```bash
sudo rm -rf /opt/upload-appointments
sudo rm -f /tmp/deploy.sh
```

## 2. Run the installer

Download and run the deployment script:

```bash
wget -O /tmp/deploy.sh https://raw.githubusercontent.com/uon-chqi/upload-appointments/main/deploy.sh && sudo bash /tmp/deploy.sh
```

You will be asked for three values:

| Prompt | What to enter |
|---|---|
| `Secrets passphrase` | The deployment passphrase provided by your administrator. |
| `OpenMRS DB User` | The MySQL username for the OpenMRS database (e.g. `root`). |
| `OpenMRS DB Password` | The password for that MySQL user. |

> If you are re-running the installer over an existing install, you may also be
> asked **`Overwrite it? (y/N)`** for the existing configuration file. Choose
> `y` to re-enter the database details, or `N` to keep the current ones.

Everything else — the Ushauri DIFF platform credentials, the admin login, and
all standard settings — is configured automatically from the encrypted
secrets file.

The installer takes a few minutes and will:

1. Install system dependencies.
2. Download the application code.
3. Set up a Python virtual environment.
4. Decrypt the shared secrets and write the configuration file.
5. Set up the application database (run migrations).
6. Collect the static web files.
7. Create the admin user.
8. Start the background service on port **9162**.
9. Schedule the daily upload (cron, 11:00 PM).

When it finishes you will see a `Deployment complete!` summary.

## 3. Verify the installation

Confirm the service is running:

```bash
sudo systemctl status upload-appointments
```

You should see `active (running)`. Press `q` to exit.

## 4. Log in and monitor uploads

Open the dashboard in a web browser:

- On the server itself: <http://127.0.0.1:9162>

Log in with:

- **Username:** `admin`
- **Password:** provided by your administrator

From the dashboard you can trigger an upload manually and watch its progress.

## Daily automatic upload

A scheduled job (cron) runs the upload automatically every day at **11:00 PM**
and uploads the previous day's appointments. No action is needed for this to
happen.

To check what the daily upload did, view its log:

```bash
tail -n 100 /var/log/upload-appointments.log
```

## Updating the service

To update to the latest version later, simply re-run the installer:

```bash
wget -O /tmp/deploy.sh https://raw.githubusercontent.com/uon-chqi/upload-appointments/main/deploy.sh && sudo bash /tmp/deploy.sh
```

It pulls the latest code and restarts the service. When asked
`Overwrite it? (y/N)`, choose `N` to keep your existing database settings.

## Managing the service

```bash
sudo systemctl status upload-appointments     # Check service status
sudo systemctl restart upload-appointments    # Restart the service
sudo systemctl stop upload-appointments       # Stop the service
sudo systemctl start upload-appointments      # Start the service
sudo journalctl -u upload-appointments -f     # View live service logs
```

## Troubleshooting

**`could not decrypt secrets.env.gpg (wrong passphrase?)`**
The secrets passphrase was entered incorrectly. Re-run the installer and enter
the correct passphrase exactly as provided by your administrator.

**The dashboard does not open in the browser**
Check the service is running with `sudo systemctl status upload-appointments`.
If it is not, view the error with
`sudo journalctl -u upload-appointments -n 50`. Also confirm no other program
is using port 9162.

**Uploads fail or report database connection errors**
Check `/var/log/upload-appointments.log`. The most common cause is incorrect
OpenMRS database credentials — re-run the installer, choose `y` at the
`Overwrite it? (y/N)` prompt, and re-enter the correct user and password.

**`Error: Please run this script as root (sudo).`**
Run the installer with `sudo`, exactly as shown in step 2.
