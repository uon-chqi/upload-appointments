import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta

import requests
from django.conf import settings
from django.db.models import F, Q, Sum
from django.utils import timezone

from . import openmrs
from .models import Facility, UploadLog, UploadRun

logger = logging.getLogger(__name__)

# Windows process-creation flags; the POSIX equivalent is start_new_session.
_DETACHED_PROCESS = 0x00000008
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


def upload_patients(patients, tokens, log=None, batch_size=None, max_retries=4,
                    progress_interval=3.0):
    """POST patient data to the Ushauri DIFF platform in batches.

    Progress is written to `log` at most once every `progress_interval` seconds
    (and always on the final batch). Saving after every batch of ten would have a
    hundred facilities hammering one SQLite file for the whole run.
    """
    import math
    batch_size = batch_size or settings.UPLOAD_BATCH_SIZE
    url = f"{settings.CHQI_API_BASE_URL}/api/patients/upload-json"
    total_batches = math.ceil(len(patients) / batch_size)
    if log:
        log.batches_total = total_batches
        log.batches_completed = 0
        log.save(update_fields=['batches_total', 'batches_completed'])

    results = []
    # Seed from the clock, not zero: monotonic() is time-since-boot, so a zero
    # seed would make the first batch always look overdue for a save.
    last_saved = time.monotonic()
    for i in range(0, len(patients), batch_size):
        batch = patients[i:i + batch_size]
        batch_num = i // batch_size + 1
        logger.info("Uploading batch %d/%d (%d–%d of %d patients)",
                     batch_num, total_batches, i + 1, i + len(batch), len(patients))
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
            if batch_num == total_batches or now - last_saved >= progress_interval:
                log.batches_completed = batch_num
                log.save(update_fields=['batches_completed'])
                last_saved = now
    return results


def upload_facility(log, config, tokens):
    """Query one facility and upload its appointments, recording the outcome on `log`."""
    log.status = 'in_progress'
    log.started_at = timezone.now()
    log.error_message = ''
    log.save(update_fields=['status', 'started_at', 'error_message'])

    try:
        with openmrs.connect(config) as conn:
            patients = openmrs.fetch_appointments(conn, log.date_from, log.date_to)

        log.records_uploaded = len(patients)
        log.save(update_fields=['records_uploaded'])

        if patients:
            upload_patients(patients, tokens, log=log)
        else:
            log.error_message = 'No records found for the given period.'

        log.status = 'success'
        logger.info("Upload successful for %s: %d records for %s to %s",
                    config.label, len(patients), log.date_from, log.date_to)
    except Exception:
        log.error_message = traceback.format_exc()
        log.status = 'failed'
        logger.error("Upload failed for %s (%s to %s): %s",
                     config.label, log.date_from, log.date_to, log.error_message)

    log.finished_at = timezone.now()
    log.save(update_fields=['status', 'error_message', 'finished_at'])
    return log


def create_run(date_from, date_to, triggered_by, user=None, mode='single',
               facilities=None, retry_of=None):
    """Create an UploadRun and one pending UploadLog per target facility.

    Materialising the child logs up front is what makes the progress endpoint and
    "retry failed facilities" trivial: the runner just processes the rows it finds.
    """
    if mode == 'multi':
        if facilities is None:
            facilities = list(Facility.objects.filter(is_active=True))
        else:
            facilities = list(facilities)
    else:
        facilities = [None]

    run = UploadRun.objects.create(
        date_from=date_from,
        date_to=date_to,
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
            date_from=date_from,
            date_to=date_to,
            triggered_by=triggered_by,
            triggered_by_user=user,
            status='pending',
        )
        for facility in facilities
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
        return upload_facility(log, config, tokens)
    finally:
        connections.close_all()


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

    totals = run.logs.aggregate(total=Sum('records_uploaded'))
    run.status = status
    run.facilities_completed = len(statuses)
    run.facilities_failed = failed
    run.records_uploaded = totals['total'] or 0
    run.finished_at = timezone.now()
    run.save(update_fields=[
        'status', 'facilities_completed', 'facilities_failed',
        'records_uploaded', 'finished_at',
    ])
    return run


def execute_run(run, workers=None):
    """Upload every facility attached to `run`, in parallel when there is more than one."""
    now = timezone.now()
    UploadRun.objects.filter(pk=run.pk).update(
        status='in_progress', started_at=now, heartbeat_at=now,
    )
    run.refresh_from_db()

    logs = list(run.logs.select_related('facility').order_by('facility_label', 'pk'))
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
                    upload_facility(log, config, tokens)
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
        kwargs['creationflags'] = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP
    else:
        kwargs['start_new_session'] = True

    subprocess.Popen(command, **kwargs)
    logger.info('Spawned detached upload process for run %s.', run_pk)
