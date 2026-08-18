"""Discovery for multi-tenant servers: one MySQL instance, many OpenMRS schemas.

In the multi-facility setup an operator types in one connection per facility. A
multi-tenant cloud server inverts that: a single MySQL holds a schema per
facility, so the only thing that differs between facilities is the schema name —
and the server itself knows the list. Syncing therefore does three things:

1. asks the server which schemas start with the configured prefix,
2. asks each of those schemas who it is, using the same `kenyaemr.defaultLocation`
   probe the multi-facility "test connection" button uses, so what is shown here
   is exactly what an upload would send,
3. reconciles the answers into `Facility` rows, which is all the upload pipeline
   ever looks at — runs, retries and backfill stamps then work unchanged.

A newly discovered schema is created disabled: sync says what is there, an
operator says what uploads. Nothing an operator has not switched on is ever
switched on by a later sync.

Only step 2 is parallel, and it touches MySQL alone: every ORM write happens on
the calling thread afterwards, so there are no worker DB connections to clean up
and a half-finished probe can never leave a facility half-written.
"""
import logging
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import openmrs
from .models import Facility, TenantServer

logger = logging.getLogger(__name__)

NAME_MAX_LENGTH = Facility._meta.get_field('name').max_length


def _summary():
    return {
        'ok': False,
        'message': '',
        'databases': 0,
        'added': 0,
        'updated': 0,
        'reappeared': 0,
        'disappeared': 0,
        'unidentified': 0,
        'problems': [],
    }


def _stamp(server, ok, message):
    """Record the outcome on the server row without touching updated_at."""
    TenantServer.objects.filter(pk=server.pk).update(
        last_synced_at=timezone.now(), last_sync_ok=ok, last_sync_message=message,
    )
    server.last_synced_at = timezone.now()
    server.last_sync_ok = ok
    server.last_sync_message = message


def _probe_all(server, databases, workers=None):
    """Identify each schema concurrently, returning {database: probe result}.

    A hundred schemas served one at a time would keep an operator waiting on a
    page load; the probe is two indexed queries, so the connections are the cost
    and running them in parallel removes it. Nothing in here touches the ORM.
    """
    if not databases:
        return {}

    password = server.get_password()  # Decrypt once, on this thread.

    def probe(database):
        config = openmrs.FacilityConfig(
            label='{}/{}'.format(server.name, database),
            host=server.host,
            port=server.port,
            user=server.username,
            password=password,
            database=database,
        )
        try:
            return openmrs.probe(config)
        except Exception as exc:  # pragma: no cover - probe catches its own
            logger.exception('Probing %s failed', database)
            return {
                'ok': False, 'message': 'Probe failed: {}'.format(exc),
                'mfl': '', 'facility_name': '', 'candidates': [], 'mysql_version': '',
            }

    count = min(len(databases), workers or settings.TENANT_PROBE_WORKERS)
    if count <= 1:
        return {db: probe(db) for db in databases}

    with ThreadPoolExecutor(max_workers=count) as pool:
        return dict(zip(databases, pool.map(probe, databases)))


def _unique_name(preferred, database_name, taken):
    """A display name for a discovered schema, unique across every facility.

    `Facility.name` is unique deployment-wide, and two tenants can genuinely
    carry the same location name, so the schema name is the tie-breaker — it is
    unique on the server by definition.
    """
    base = (preferred or '').strip() or database_name
    candidates = [
        base,
        '{} ({})'.format(base, database_name),
        database_name,
    ]
    for candidate in candidates:
        candidate = candidate[:NAME_MAX_LENGTH]
        if candidate not in taken:
            return candidate

    suffix = 2
    while True:
        candidate = '{} ({})'.format(database_name, suffix)[:NAME_MAX_LENGTH]
        if candidate not in taken:
            return candidate
        suffix += 1


def _disable(facility, message):
    """Take a facility out of the upload set because sync could not vouch for it.

    Uploading under a blank or duplicated MFL is worse than not uploading: the
    DIFF platform keys on MFL alone, so the records would land on the wrong
    facility upstream or be rejected. `disabled_by_sync` records that this was
    sync's doing, so a later sync may undo it — an operator switching a facility
    off by hand is left alone.
    """
    facility.is_active = False
    facility.disabled_by_sync = True
    facility.last_test_ok = False
    facility.last_test_message = message


def sync_server(server, workers=None, reprobe=True):
    """Reconcile one server's schemas into Facility rows.

    `reprobe=False` identifies only schemas that are new or still unidentified,
    which is what a nightly sync wants: the MFL of a container that already
    answered is not going to change, and skipping it keeps the run short.
    """
    summary = _summary()

    try:
        databases = openmrs.list_databases(server.as_config(), server.database_prefix)
    except Exception as exc:
        message = 'Could not list databases on {}: {}'.format(server.host, exc)
        _stamp(server, ok=False, message=message)
        summary['message'] = message
        logger.error('Tenant sync failed for %s: %s', server.name, exc)
        return summary

    summary['databases'] = len(databases)
    existing = {f.database_name: f for f in server.facilities.all()}
    discovered = set(databases)

    # Retire the schemas that never came back from SHOW DATABASES first, so a
    # renamed schema can hand its MFL to its replacement in the same sync
    # instead of colliding with the row it superseded. Deactivated rather than
    # deleted: the schema may be temporarily gone, and the upload history
    # pointing at it is worth keeping either way.
    for database in [db for db in existing if db not in discovered]:
        facility = existing.pop(database)
        summary['disappeared'] += 1
        if not facility.is_active and facility.disabled_by_sync and not facility.mfl_code:
            continue
        facility.mfl_code = ''
        _disable(facility, 'Database "{}" is no longer on {}.'.format(
            database, server.name,
        ))
        facility.save(update_fields=[
            'mfl_code', 'is_active', 'disabled_by_sync', 'last_test_ok',
            'last_test_message', 'updated_at',
        ])

    to_probe = [
        db for db in databases
        if reprobe or db not in existing or not existing[db].mfl_code
    ]
    probes = _probe_all(server, to_probe, workers=workers)

    # Every MFL already claimed elsewhere: other servers, standalone facilities,
    # and — as the loop runs — the schemas already reconciled on this one. Two
    # facilities sharing an MFL would overwrite each other upstream.
    claimed = {
        mfl: label
        for mfl, label in Facility.objects
        .exclude(server=server).exclude(mfl_code='')
        .values_list('mfl_code', 'name')
    }
    taken_names = set(Facility.objects.values_list('name', flat=True))
    now = timezone.now()

    for database in databases:
        facility = existing.get(database)
        result = probes.get(database)
        is_new = facility is None

        if is_new:
            facility = Facility(server=server, database_name=database)
            facility.password_encrypted = ''  # Credentials belong to the server.
            # Discovered is not the same as wanted. A server may hold schemas
            # that are demos, archives, or simply not this deployment's to
            # upload, and at a hundred of them an operator cannot be expected to
            # notice one that switched itself on. So a new schema is listed and
            # left off; `activated_at` stays NULL until somebody enables it.
            facility.is_active = False
            facility.disabled_by_sync = False
        else:
            taken_names.discard(facility.name)

        # Denormalised for display only; as_config() always reads the server.
        facility.host = server.host
        facility.port = server.port
        facility.username = server.username
        facility.last_seen_at = now

        was_disabled_by_sync = facility.disabled_by_sync
        # The name sync would have given this row last time. Only that one is
        # replaced, so an operator's rename survives every later sync.
        derived_name = facility.mfl_facility_name or facility.database_name

        if result is None:
            # Already identified and not re-probing: leave the identity alone.
            pass
        elif not result['ok']:
            # The MFL has to go — an unverifiable one must not reach the platform
            # — but the name it last reported stays, so a container that is down
            # for a minute does not lose its label and get renamed when it
            # returns under a different one.
            facility.mfl_code = ''
            facility.last_tested_at = now
            _disable(facility, result['message'])
            summary['unidentified'] += 1
            summary['problems'].append('{}: {}'.format(database, result['message']))
        else:
            owner = claimed.get(result['mfl'])
            facility.last_tested_at = now
            if owner is not None:
                message = (
                    'MFL {} is already used by "{}". The DIFF platform identifies '
                    'facilities by MFL, so these two would overwrite each '
                    'other.'.format(result['mfl'], owner)
                )
                facility.mfl_code = ''
                facility.mfl_facility_name = result['facility_name']
                _disable(facility, message)
                summary['unidentified'] += 1
                summary['problems'].append('{}: {}'.format(database, message))
            else:
                claimed[result['mfl']] = result['facility_name'] or database
                facility.mfl_code = result['mfl']
                facility.mfl_facility_name = result['facility_name']
                facility.last_test_ok = True
                facility.last_test_message = result['message']
                # Sync only ever switches back on what an operator switched
                # on before and sync itself took away. One that was never
                # enabled stays off however cleanly it now identifies.
                if was_disabled_by_sync and facility.activated_at:
                    facility.is_active = True
                    facility.disabled_by_sync = False
                    summary['reappeared'] += 1

        # Name a row when it is new, or when a successful probe has a better name
        # than the one sync itself last derived. A failed probe never renames:
        # "Kilifi Dispensary" reverting to "openmrs_kilifi" because the container
        # was down for a minute would be churn, not information.
        if is_new or (facility.mfl_facility_name and facility.name == derived_name):
            facility.name = _unique_name(
                facility.mfl_facility_name, database, taken_names,
            )
        taken_names.add(facility.name)

        try:
            with transaction.atomic():
                facility.save()
        except IntegrityError as exc:
            # A concurrent sync or a hand-edited facility got there first. One
            # schema failing must not abandon the other ninety-nine.
            taken_names.discard(facility.name)
            summary['problems'].append('{}: {}'.format(database, exc))
            logger.warning('Could not save tenant %s on %s: %s',
                           database, server.name, exc)
            continue

        if is_new:
            summary['added'] += 1
        else:
            summary['updated'] += 1

    summary['ok'] = True
    summary['message'] = (
        '{} database(s) found — {} added (disabled until enabled), {} updated, '
        '{} gone, {} could not be identified.'.format(
            summary['databases'], summary['added'], summary['updated'],
            summary['disappeared'], summary['unidentified'],
        )
    )
    _stamp(server, ok=not summary['problems'], message=summary['message'])
    logger.info('Tenant sync for %s: %s', server.name, summary['message'])
    return summary


def sync_all(workers=None, reprobe=True):
    """Sync every active server, returning [(server, summary)].

    One server being unreachable must not stop the others: each summary carries
    its own outcome and the caller decides what that means.
    """
    results = []
    for server in TenantServer.objects.filter(is_active=True):
        results.append((server, sync_server(server, workers=workers, reprobe=reprobe)))
    return results
