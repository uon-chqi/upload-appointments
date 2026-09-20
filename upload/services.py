import json
import logging
import math
import os
import random
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.db.models import F, Q, Sum
from django.utils import timezone

from . import openmrs, schedule
from .models import AppSettings, Facility, UploadLog, UploadRun

logger = logging.getLogger(__name__)

# Windows process-creation flags; the POSIX equivalent is start_new_session.
#
# CREATE_NO_WINDOW, not DETACHED_PROCESS: despite reading as though it leaves the
# child console-less, DETACHED_PROCESS makes Windows allocate the child a fresh
# console, which appears as a blank window for as long as the upload runs (an
# hour, at a hundred facilities). CREATE_NO_WINDOW gives a console-subsystem
# program no console at all — measured with GetConsoleWindow(), which returns 0
# under this flag and a live handle under DETACHED_PROCESS. Either way the child
# is off the parent's console and outlives it; only this one is invisible.
# The two are mutually exclusive: combining them makes Windows ignore
# CREATE_NO_WINDOW and the window comes back.
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_PROCESS_GROUP = 0x00000200


def get_api_token():
    """Authenticate with the Ushauri DIFF platform and return a bearer token."""
    url = f"{settings.CHQI_API_BASE_URL}/api/auth/login"
    username = settings.CHQI_API_USERNAME
    password = settings.CHQI_API_PASSWORD
    if not username or not password:
        raise ValueError(
            f"Ushauri DIFF platform credentials not configured "
            f"(username={'set' if username else 'MISSING'}, "
            f"password={'set' if password else 'MISSING'})"
        )
    max_retries = 3
    retry_delay = 10  # seconds
    for attempt in range(1, max_retries + 1):
        response = requests.post(
            url,
            data=json.dumps({
                'username': username,
                'password': password,
            }),
            headers={'Content-Type': 'application/json'},
            timeout=30,
        )
        if response.ok:
            break
        try:
            detail = response.json()
        except Exception:
            detail = response.text
        logger.error("Login attempt %d/%d failed (HTTP %s): %s",
                      attempt, max_retries, response.status_code, detail)
        # Retry on server errors (5xx), not client errors (4xx)
        if response.status_code >= 500 and attempt < max_retries:
            time.sleep(retry_delay * attempt)
            continue
        response.raise_for_status()
    data = response.json()
    logger.info("Login response keys: %s", list(data.keys()) if isinstance(data, dict) else data)
    # Try common token field names; search nested structures too
    token = (
        data.get('token')
        or data.get('access_token')
        or data.get('accessToken')
        or (data.get('data', {}) or {}).get('token')
        or (data.get('data', {}) or {}).get('access_token')
        or (data.get('data', {}) or {}).get('accessToken')
    )
    if not token:
        raise ValueError(f"Could not find token in login response: {data}")
    return token


class TokenProvider:
    """Hands one bearer token to every facility in a run, re-authenticating on 401.

    All facilities upload under a single DIFF account, so logging in once per run
    rather than once per facility saves ~100 round trips. But a hundred facilities
    at five workers takes north of half an hour, which is long enough for a token
    to expire mid-run — hence `refresh`, which re-authenticates exactly once no
    matter how many workers hit a 401 at the same moment.
    """

    def __init__(self, fetch=None):
        self._fetch = fetch or get_api_token
        self._lock = threading.Lock()
        self._token = None

    def get(self):
        with self._lock:
            if self._token is None:
                self._token = self._fetch()
            return self._token

    def refresh(self, stale_token):
        """Replace `stale_token`, unless another thread already did."""
        with self._lock:
            if self._token is not None and self._token != stale_token:
                return self._token
            logger.info('Re-authenticating with the DIFF platform (token rejected).')
            self._token = self._fetch()
            return self._token


def _backoff_delay(base_delay, attempt, cap=120):
    """Exponential backoff with full jitter.

    Returns a random delay in [0, min(cap, base_delay * 2**(attempt-1))]. The
    jitter is what matters when ~250 facilities can hit the central API at
    once: fixed backoff would just re-synchronise their retries into a second
    stampede, whereas randomising the wait spreads them back out.
    """
    ceiling = min(cap, base_delay * (2 ** (attempt - 1)))
    return random.uniform(0, ceiling)


def _post_batch_with_retry(url, payload, tokens, batch_num, total_batches,
                           max_retries=4, base_delay=5, timeout=120, max_reauths=1):
    """POST one batch, retrying transient failures with backoff + jitter.

    Retries on connection errors, timeouts, HTTP 429, and 5xx responses. A 401 is
    treated as an expired token: re-authenticate once and retry immediately,
    without spending a transient-failure attempt or sleeping. Raises immediately
    on other client errors and once retries are exhausted.
    """
    attempt = 0
    reauths = 0

    while True:
        token = tokens.get()
        headers = {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

        try:
            response = requests.post(url, data=payload, headers=headers, timeout=timeout)
        except (requests.ConnectionError, requests.Timeout) as exc:
            attempt += 1
            if attempt >= max_retries:
                raise
            delay = _backoff_delay(base_delay, attempt)
            logger.warning(
                "Batch %d/%d network error (attempt %d/%d): %s; retrying in %.1fs",
                batch_num, total_batches, attempt, max_retries, exc, delay,
            )
            time.sleep(delay)
            continue

        if response.ok:
            return response

        try:
            detail = response.json()
        except Exception:
            detail = response.text

        if response.status_code == 401 and reauths < max_reauths:
            reauths += 1
            logger.warning(
                "Batch %d/%d rejected with HTTP 401; refreshing token and retrying.",
                batch_num, total_batches,
            )
            tokens.refresh(token)
            continue

        attempt += 1
        retryable = response.status_code == 429 or response.status_code >= 500
        if not retryable or attempt >= max_retries:
            raise requests.HTTPError(
                f"HTTP {response.status_code}: {detail}",
                response=response,
            )

        # Honour Retry-After when the server sends it (common with 429),
        # otherwise fall back to jittered exponential backoff.
        retry_after = response.headers.get('Retry-After', '')
        if retry_after.isdigit():
            delay = int(retry_after)
        else:
            delay = _backoff_delay(base_delay, attempt)
        logger.warning(
            "Batch %d/%d failed HTTP %s (attempt %d/%d): %s; retrying in %.1fs",
            batch_num, total_batches, response.status_code,
            attempt, max_retries, detail, delay,
        )
        time.sleep(delay)


def _upload_records(url, records, tokens, log=None, batch_size=None, max_retries=4,
                    progress_interval=3.0, batch_offset=0, batches_total=None):
    """POST records to `url` in batches of `batch_size`.

    Progress is written to `log` at most once every `progress_interval` seconds
    (and always on this phase's final batch). Saving after every batch of ten
    would have a hundred facilities hammering one SQLite file for the whole run.

    `batch_offset` and `batches_total` let the two phases of one facility —
    appointments, then patient updates — share a single progress bar: the second
    phase carries on from the first's batch number instead of restarting at
    zero, so the bar never jumps backwards mid-facility.
    """
    batch_size = batch_size or settings.UPLOAD_BATCH_SIZE
    own_batches = math.ceil(len(records) / batch_size)
    last_batch = batch_offset + own_batches
    total_batches = own_batches if batches_total is None else batches_total
    if log:
        log.batches_total = total_batches
        log.batches_completed = batch_offset
        log.save(update_fields=['batches_total', 'batches_completed'])

    results = []
    # Seed from the clock, not zero: monotonic() is time-since-boot, so a zero
    # seed would make the first batch always look overdue for a save.
    last_saved = time.monotonic()
    for i in range(0, len(records), batch_size):
        batch = records[i:i + batch_size]
        batch_num = batch_offset + i // batch_size + 1
        logger.info("Uploading batch %d/%d (%d–%d of %d records) to %s",
                    batch_num, total_batches, i + 1, i + len(batch), len(records), url)
        response = _post_batch_with_retry(
            url,
            json.dumps({'patients': batch}),
            tokens,
            batch_num,
            total_batches,
            max_retries=max_retries,
        )
        results.append(response.json())

        if log:
            now = time.monotonic()
            if batch_num == last_batch or now - last_saved >= progress_interval:
                log.batches_completed = batch_num
                log.save(update_fields=['batches_completed'])
                last_saved = now
    return results


def upload_patients(patients, tokens, log=None, **kwargs):
    """Send appointment records to the DIFF platform."""
    url = f"{settings.CHQI_API_BASE_URL}/api/patients/upload-json"
    return _upload_records(url, patients, tokens, log=log, **kwargs)


def upload_patient_updates(patients, tokens, log=None, **kwargs):
    """Send the between-visit patient details to the DIFF platform.

    A separate endpoint, and a separate table upstream: these rows carry only
    what can have changed since the appointment was uploaded, and are appended
    rather than overwriting, so a patient's risk and lab history is kept.
    """
    url = f"{settings.CHQI_API_BASE_URL}/api/patients/update-json"
    return _upload_records(url, patients, tokens, log=log, **kwargs)


def upload_facility(log, config, tokens, backfill=False):
    """Query one facility and upload it, recording the outcome on `log`.

    Two uploads per facility, in order: the appointments for the period, then the
    patient details that can have changed since those appointments were first
    sent. The second is not bounded by the period and happens on every run — an
    appointment uploaded last month is still what the platform is working from,
    and its phone number, risk score and labs are what go stale.

    Either failing fails the facility, which is what the "Retry failed" button
    re-runs. Upstream appends rather than overwrites, so a retry after a
    half-finished upload duplicates rather than corrupts.

    `backfill` drops the date window and sends every pending appointment — the
    one-off initial load. On success it stamps the facility, so a run that leaves
    a few containers behind shows exactly which ones still need it.
    """
    log.status = 'in_progress'
    log.started_at = timezone.now()
    log.error_message = ''
    log.save(update_fields=['status', 'started_at', 'error_message'])

    period = 'all pending' if backfill else '{} to {}'.format(log.date_from, log.date_to)
    try:
        # Both queries run on one connection: opening a second one per facility
        # would double the connection count a hundred containers present at once.
        with openmrs.connect(config) as conn:
            if backfill:
                patients = openmrs.fetch_appointments(conn)
            else:
                patients = openmrs.fetch_appointments(conn, log.date_from, log.date_to)
            # Unaffected by the date window or by `backfill`: the patient-detail
            # refresh always covers everybody with a pending appointment.
            updates = openmrs.fetch_patient_updates(conn)

        log.records_uploaded = len(patients)
        log.patient_updates_uploaded = len(updates)
        log.save(update_fields=['records_uploaded', 'patient_updates_uploaded'])

        # The two phases share one progress bar, so the batch count is worked out
        # across both before either starts.
        batch_size = settings.UPLOAD_BATCH_SIZE
        appointment_batches = math.ceil(len(patients) / batch_size)
        total_batches = appointment_batches + math.ceil(len(updates) / batch_size)

        if patients:
            upload_patients(patients, tokens, log=log, batches_total=total_batches)
        if updates:
            upload_patient_updates(
                updates, tokens, log=log,
                batch_offset=appointment_batches, batches_total=total_batches,
            )
        if not patients and not updates:
            log.error_message = 'No records found for the given period.'

        log.status = 'success'
        if backfill and log.facility_id:
            Facility.objects.filter(pk=log.facility_id).update(
                initial_backfill_at=timezone.now(),
            )
        logger.info("Upload successful for %s: %d records for %s, %d patient update(s)",
                    config.label, len(patients), period, len(updates))
    except Exception:
        log.error_message = traceback.format_exc()
        log.status = 'failed'
        logger.error("Upload failed for %s (%s): %s",
                     config.label, period, log.error_message)

    log.finished_at = timezone.now()
    log.save(update_fields=['status', 'error_message', 'finished_at'])
    # Move the catch-up watermark on, so the next tick asks for the right period
    # and a facility that failed keeps owing what it owed. A backfill covered
    # everything outstanding, so from here only today's bookings are new.
    schedule.record_attempt(
        log.facility_id,
        ok=log.status == 'success',
        synced_through=timezone.localdate() if backfill else log.date_to,
    )
    return log


def create_run(date_from, date_to, triggered_by, user=None, mode='single',
               facilities=None, retry_of=None, is_backfill=False, windows=None):
    """Create an UploadRun and one pending UploadLog per target facility.

    Materialising the child logs up front is what makes the progress endpoint and
    "retry failed facilities" trivial: the runner just processes the rows it finds.

    `windows` gives each facility a period of its own, which is what a catch-up
    run needs: one container may be a day behind and the next a week, and one
    nobody has ever uploaded needs no window at all but every pending
    appointment. It is a callable taking a facility — or None for the
    environment-configured one — and returning `(date_from, date_to)`, or None
    to mean "everything pending". Without it every log takes the run's own
    dates, which is what a manual upload and the nightly window both want.
    """
    if mode in UploadRun.MULTI_MODES:
        if facilities is None:
            # Scoped by mode: standalone containers and discovered tenants share
            # one table but never one run.
            facilities = list(Facility.objects.for_mode(mode).filter(is_active=True))
        else:
            facilities = list(facilities)
    else:
        facilities = [None]

    # Worked out before the run row exists, because the run's own dates are the
    # envelope of its logs': a run whose worst-off facility is six days behind
    # reads as six days, and the facility that is only a day behind still gets
    # asked for a day.
    periods = []
    for facility in facilities:
        window = windows(facility) if windows else None
        if windows is None:
            periods.append((facility, date_from, date_to, is_backfill))
        elif window is None:
            periods.append((facility, date_from, date_to, True))
        else:
            periods.append((facility, window[0], window[1], False))

    if periods:
        run_from = min(period[1] for period in periods)
        run_to = max(period[2] for period in periods)
        # A run is a backfill only when every facility in it is. The flag drives
        # `period_label` and the deployment-wide initial-load record, and a run
        # where one container out of a hundred needed a full load is not the
        # initial load of the deployment.
        run_backfill = all(period[3] for period in periods)
    else:
        run_from, run_to, run_backfill = date_from, date_to, is_backfill

    run = UploadRun.objects.create(
        date_from=run_from,
        date_to=run_to,
        is_backfill=run_backfill,
        mode=mode,
        triggered_by=triggered_by,
        triggered_by_user=user,
        status='pending',
        facilities_total=len(facilities),
        retry_of=retry_of,
    )

    env_label = openmrs.env_config().label
    UploadLog.objects.bulk_create([
        UploadLog(
            run=run,
            facility=facility,
            facility_label=facility.name if facility else env_label,
            date_from=log_from,
            date_to=log_to,
            is_backfill=log_backfill,
            triggered_by=triggered_by,
            triggered_by_user=user,
            status='pending',
        )
        for facility, log_from, log_to, log_backfill in periods
    ])
    return run


def _heartbeat_loop(run_pk, stop_event, interval=30):
    """Tick the run's heartbeat until told to stop, so it isn't judged stale."""
    from django.db import connections
    try:
        while not stop_event.wait(interval):
            UploadRun.objects.filter(pk=run_pk).update(heartbeat_at=timezone.now())
    finally:
        connections.close_all()


def _facility_worker(log_pk, config, tokens):
    """Run one facility in its own thread, closing that thread's DB connections."""
    from django.db import connections
    try:
        log = UploadLog.objects.get(pk=log_pk)
        return upload_facility(log, config, tokens, backfill=log.is_backfill)
    finally:
        connections.close_all()


def _record_backfill_done(run):
    """Mark the deployment's one-off initial load as having happened.

    Deliberately also on a `partial` run: at a hundred facilities a couple are
    always unreachable, and repeating a full-history upload for the other
    ninety-eight every night to chase them would be worse than leaving them to
    the retry button, which re-runs the backfill for exactly those facilities.
    """
    app_settings = AppSettings.load()
    if app_settings.initial_backfill_done:
        return

    # Only a run that covered every active facility counts as *the* initial load.
    # `--facility 7` and "retry failed" are backfills too, and neither says
    # anything about the ninety-nine containers they didn't touch.
    if run.mode in UploadRun.MULTI_MODES:
        active = set(
            Facility.objects.for_mode(run.mode)
            .filter(is_active=True).values_list('pk', flat=True)
        )
        covered = set(
            run.logs.exclude(facility__isnull=True).values_list('facility_id', flat=True)
        )
        if not active.issubset(covered):
            logger.info(
                'Backfill run %s covered %d of %d active facilities; leaving the '
                'initial load flag unset.', run.pk, len(covered & active), len(active),
            )
            return
    app_settings.initial_backfill_done = True
    app_settings.initial_backfill_at = timezone.now()
    app_settings.initial_backfill_run = run
    app_settings.save(update_fields=[
        'initial_backfill_done', 'initial_backfill_at', 'initial_backfill_run',
        'updated_at',
    ])
    logger.info('Initial backfill recorded as complete (run %s, %s).',
                run.pk, run.status)


def _finalize_run(run):
    statuses = list(run.logs.values_list('status', flat=True))
    failed = sum(1 for s in statuses if s == 'failed')
    succeeded = sum(1 for s in statuses if s == 'success')

    if not statuses:
        status = 'failed'
    elif failed == 0:
        status = 'success'
    elif succeeded == 0:
        status = 'failed'
    else:
        status = 'partial'

    totals = run.logs.aggregate(
        total=Sum('records_uploaded'),
        updates=Sum('patient_updates_uploaded'),
    )
    run.status = status
    run.facilities_completed = len(statuses)
    run.facilities_failed = failed
    run.records_uploaded = totals['total'] or 0
    run.patient_updates_uploaded = totals['updates'] or 0
    run.finished_at = timezone.now()
    run.save(update_fields=[
        'status', 'facilities_completed', 'facilities_failed',
        'records_uploaded', 'patient_updates_uploaded', 'finished_at',
    ])
    if run.is_backfill and status in ('success', 'partial'):
        _record_backfill_done(run)
    return run


def execute_run(run, workers=None):
    """Upload every facility attached to `run`, in parallel when there is more than one."""
    now = timezone.now()
    UploadRun.objects.filter(pk=run.pk).update(
        status='in_progress', started_at=now, heartbeat_at=now,
    )
    run.refresh_from_db()

    # facility__server too: a tenant's credentials live on its server, and
    # fetching them lazily would be a query per facility.
    logs = list(
        run.logs.select_related('facility', 'facility__server')
        .order_by('facility_label', 'pk')
    )
    if not logs:
        run.message = 'No active facilities are configured.'
        run.save(update_fields=['message'])
        return _finalize_run(run)

    # Resolve configs on the main thread: decryption stays off the workers, and a
    # facility with an unreadable password fails cleanly instead of mid-upload.
    targets = []
    for log in logs:
        try:
            config = log.facility.as_config() if log.facility_id else openmrs.env_config()
        except ValueError as exc:
            log.status = 'failed'
            log.error_message = str(exc)
            log.finished_at = timezone.now()
            log.save(update_fields=['status', 'error_message', 'finished_at'])
            # An attempt that never got as far as connecting is still an attempt:
            # without this the facility stays due and is retried every tick.
            schedule.record_attempt(log.facility_id, ok=False)
            continue
        targets.append((log, config))

    if targets:
        tokens = TokenProvider()
        worker_count = max(1, min(len(targets), workers or settings.MULTI_FACILITY_WORKERS))

        stop_heartbeat = threading.Event()
        heartbeat = threading.Thread(
            target=_heartbeat_loop, args=(run.pk, stop_heartbeat), daemon=True,
        )
        heartbeat.start()
        try:
            if worker_count == 1:
                for log, config in targets:
                    upload_facility(log, config, tokens, backfill=log.is_backfill)
                    _record_facility_done(run.pk)
            else:
                logger.info('Uploading %d facilities with %d workers.',
                            len(targets), worker_count)
                with ThreadPoolExecutor(max_workers=worker_count) as pool:
                    futures = {
                        pool.submit(_facility_worker, log.pk, config, tokens): log
                        for log, config in targets
                    }
                    for future in as_completed(futures):
                        log = futures[future]
                        try:
                            future.result()
                        except Exception:
                            # upload_facility swallows its own errors, so reaching
                            # here means the worker itself broke.
                            logger.exception('Worker crashed for facility %s', log.facility_label)
                            UploadLog.objects.filter(pk=log.pk).update(
                                status='failed',
                                error_message=traceback.format_exc(),
                                finished_at=timezone.now(),
                            )
                            schedule.record_attempt(log.facility_id, ok=False)
                        _record_facility_done(run.pk)
        finally:
            stop_heartbeat.set()
            heartbeat.join(timeout=5)

    run.refresh_from_db()
    return _finalize_run(run)


def _record_facility_done(run_pk):
    """Bump the run's live counters. Called from the main thread only."""
    UploadRun.objects.filter(pk=run_pk).update(
        facilities_completed=F('facilities_completed') + 1,
        heartbeat_at=timezone.now(),
    )


def mark_stale_runs():
    """Fail runs whose process died without finishing.

    A run is driven by a detached subprocess; if that process is killed the run
    would otherwise sit at in_progress forever and block every later upload.
    """
    cutoff = timezone.now() - timedelta(minutes=settings.UPLOAD_STALE_MINUTES)
    stale = UploadRun.objects.filter(
        status__in=UploadRun.ACTIVE_STATUSES,
    ).filter(
        Q(heartbeat_at__lt=cutoff)
        | Q(heartbeat_at__isnull=True, created_at__lt=cutoff)
    )

    for run in stale:
        message = (
            'Upload stopped unexpectedly (no progress for over {} minutes). The '
            'process was probably killed or the server restarted.'.format(
                settings.UPLOAD_STALE_MINUTES,
            )
        )
        run.logs.filter(status__in=UploadRun.ACTIVE_STATUSES).update(
            status='failed', error_message=message, finished_at=timezone.now(),
        )
        run.status = 'failed'
        run.message = message
        run.finished_at = timezone.now()
        run.save(update_fields=['status', 'message', 'finished_at'])
        logger.warning('Marked run %s stale.', run.pk)
    return stale


def active_run():
    """The run currently in flight, if any. Callers should mark_stale_runs() first."""
    return UploadRun.objects.filter(status__in=UploadRun.ACTIVE_STATUSES).first()


def platform_reachable(timeout=None):
    """Whether the DIFF platform can be reached from here. Returns (ok, why).

    A TCP connect, not a login. This runs every time the due-check ticks, and
    what it needs to know is only whether there is a working link to the platform
    at all — DNS resolves, a route exists, something is listening. Asking for a
    token instead would put a failed login in the platform's own logs every half
    hour of an outage, and spend the three retries and thirty-second timeouts in
    `get_api_token` on a question a three-second socket answers.

    The point is to keep a facility whose internet comes and goes from writing a
    failed run every tick. With no link there is nothing to record but the
    weather, and a history page of forty-eight identical connection errors is a
    history page the operator stops reading.
    """
    url = settings.CHQI_API_BASE_URL
    if not url:
        return False, 'CHQI_API_BASE_URL is not configured.'

    parsed = urlparse(url)
    if not parsed.hostname:
        return False, 'CHQI_API_BASE_URL is not a usable URL: {!r}.'.format(url)
    port = parsed.port or (443 if parsed.scheme == 'https' else 80)

    try:
        socket.create_connection(
            (parsed.hostname, port),
            timeout=timeout or settings.UPLOAD_CONNECT_CHECK_SECONDS,
        ).close()
    except OSError as exc:
        return False, 'Cannot reach {}:{} — {}.'.format(parsed.hostname, port, exc)
    return True, ''


def spawn_run(run_pk):
    """Execute a run in a detached subprocess.

    Deliberately not a thread inside gunicorn: a hundred facilities takes tens of
    minutes, and any worker restart or redeploy in that window would kill the
    upload. A detached `manage.py upload_appointments --run-id` survives, and it
    is the same code path cron uses.
    """
    command = [
        sys.executable,
        str(settings.BASE_DIR / 'manage.py'),
        'upload_appointments',
        '--run-id', str(run_pk),
    ]
    kwargs = {
        'cwd': str(settings.BASE_DIR),
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
    }
    if os.name == 'nt':
        kwargs['creationflags'] = _CREATE_NO_WINDOW | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True

    subprocess.Popen(command, **kwargs)
    logger.info('Spawned detached upload process for run %s.', run_pk)
