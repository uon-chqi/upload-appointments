"""When a facility is next due an upload, and what period it owes.

The nightly cron this supplements asks one question — "what was booked since
yesterday?" — and asks it once, for the whole deployment, at six in the morning.
That only works if every machine is switched on at six with a working link.
Facilities that power down overnight, or find their internet somewhere in the
middle of the day, need the opposite: a job that runs often, knows which
facilities still owe an upload, and asks each of them for exactly the period it
missed rather than for last night.

Three pieces of per-target state make that possible, kept where the existing
`initial_backfill_at` stamps already live — on `Facility` for a container or a
discovered tenant, and on `AppSettings` for the environment-configured facility,
which single-facility mode uploads and which has no `Facility` row at all. The
duplication is deliberate and follows `initial_backfill_at`: a single-facility
deployment must not need a Facility table it otherwise never uses.

* `appointments_synced_through` — the last day whose bookings reached the
  platform. A catch-up window starts here, so three days switched off produces a
  three-day window rather than three lost days. It only ever moves forward.
* `last_attempt_at` / `consecutive_failures` — what keeps a container that is
  simply unplugged from being retried every half hour until midnight.

Nothing here decides *how* to upload. It decides which targets a tick should
touch and what window each of them gets; `services.create_run` turns that into
a run, and `record_attempt` below moves the state on once the run has finished
with a facility.
"""

from datetime import timedelta

from django.conf import settings
from django.db.models import F, Q
from django.utils import timezone

from .models import AppSettings, Facility, UploadRun


def state_for(facility):
    """The catch-up state for a target: a `Facility`, or `None` for the env one.

    `None` is the same `None` that `create_run` and `UploadLog.facility` already
    mean by the environment-configured facility, so callers never have to say
    which of the two shapes they are in.
    """
    return facility if facility is not None else AppSettings.load()


def window_for(facility, today=None):
    """The period `facility` still owes, or None meaning "everything pending".

    `date_from` is the watermark itself rather than the day after it. The run
    that set it covered bookings up to that *date*, and anything booked later
    the same day arrived after it had looked — so re-asking for that one day is
    what closes the gap. It is also exactly the overlap the yesterday-to-today
    window always had, and the platform appends rather than overwrites, so this
    changes nothing about how much gets sent twice.

    Returning None for a target that is far enough behind is not a giving-up:
    `pending_appointments` only ever yields future, non-cancelled appointments,
    so a window of many weeks converges on the same rows the unfiltered backfill
    query returns — and that query is the cheaper of the two, having no date
    predicate to evaluate.
    """
    today = today or timezone.localdate()
    watermark = state_for(facility).appointments_synced_through
    if watermark is None:
        return None
    if today - watermark > timedelta(days=settings.UPLOAD_CATCH_UP_DAYS):
        return None
    # min(), not the watermark itself: a clock that has gone backwards must not
    # produce date_from > date_to and a query that silently matches nothing.
    return (min(watermark, today), today)


def retry_delay(consecutive_failures):
    """How long to leave a failing target alone, in minutes.

    A facility that is switched off is not a facility that is broken, but from
    here the two look identical, and at a tick every half hour the difference
    between retrying sensibly and retrying pointlessly is a history page nobody
    can read. Each successive failure doubles the wait, up to a ceiling kept low
    enough that a machine coming online at lunchtime still uploads that day.
    """
    if consecutive_failures < 1:
        return 0
    delay = settings.UPLOAD_RETRY_BASE_MINUTES * (2 ** (consecutive_failures - 1))
    return min(delay, settings.UPLOAD_RETRY_MAX_MINUTES)


def is_due(facility, now=None):
    """Whether `facility` should be uploaded at this tick."""
    now = now or timezone.now()
    state = state_for(facility)

    watermark = state.appointments_synced_through
    if watermark is not None and watermark >= timezone.localdate(now):
        # Today's bookings are already upstream. Whether that happened at 06:00
        # from cron or at 14:20 because somebody switched the machine on then
        # makes no difference: the day is covered.
        return False

    if state.last_attempt_at is None:
        return True
    delay = retry_delay(state.consecutive_failures)
    return now - state.last_attempt_at >= timedelta(minutes=delay)


def due_targets(mode, now=None):
    """Every target in `mode` that owes an upload right now.

    Single mode has exactly one, the environment-configured facility, and it is
    represented by `None` — so the caller gets `[None]` or `[]` and hands either
    straight to `create_run` without a special case.
    """
    if mode not in UploadRun.MULTI_MODES:
        return [None] if is_due(None, now) else []
    facilities = Facility.objects.for_mode(mode).filter(is_active=True)
    return [facility for facility in facilities if is_due(facility, now)]


def record_attempt(facility_id, ok, synced_through=None, now=None):
    """Write back what one upload attempt did to a target's catch-up state.

    `facility_id` is None for the environment-configured facility. A successful
    attempt clears the failure count and advances the watermark; a failed one
    leaves the watermark alone, which is the whole point — the period stays owed
    and the next successful run collects it along with everything since.

    The watermark never moves backwards. A manual upload of some week in January
    is a legitimate thing to ask for and must not convince the nightly job that
    January is where the facility has got to.
    """
    now = now or timezone.now()
    advance = ok and synced_through is not None

    if facility_id is None:
        state = AppSettings.load()
        fields = ['last_attempt_at', 'consecutive_failures', 'updated_at']
        state.last_attempt_at = now
        state.consecutive_failures = 0 if ok else state.consecutive_failures + 1
        if advance and (state.appointments_synced_through is None
                        or synced_through > state.appointments_synced_through):
            state.appointments_synced_through = synced_through
            fields.append('appointments_synced_through')
        state.save(update_fields=fields)
        return

    # Queryset updates rather than a fetch-modify-save, because this runs on the
    # upload worker threads, the same way initial_backfill_at is stamped.
    rows = Facility.objects.filter(pk=facility_id)
    rows.update(
        last_attempt_at=now,
        consecutive_failures=0 if ok else F('consecutive_failures') + 1,
    )
    if advance:
        rows.filter(
            Q(appointments_synced_through__isnull=True)
            | Q(appointments_synced_through__lt=synced_through)
        ).update(appointments_synced_through=synced_through)
