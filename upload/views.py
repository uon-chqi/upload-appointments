from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import openmrs, services, tenants
from .forms import FacilityForm, TenantServerForm, UploadForm
from .models import AppSettings, Facility, TenantServer, UploadLog, UploadRun

staff_required = user_passes_test(lambda u: u.is_active and u.is_staff)

ERROR_PREVIEW_CHARS = 300


def _serialize_run(run):
    """Everything the progress UI needs, in one response.

    One payload for the whole run rather than a poll per facility: at a hundred
    facilities the alternative is a hundred requests every couple of seconds.
    """
    logs = run.logs.order_by('facility_label', 'pk')
    return {
        'run_id': run.pk,
        'status': run.status,
        'mode': run.mode,
        'is_backfill': run.is_backfill,
        'period_label': run.period_label,
        'date_from': run.date_from.isoformat(),
        'date_to': run.date_to.isoformat(),
        'facilities_total': run.facilities_total,
        'facilities_completed': run.facilities_completed,
        'facilities_failed': run.facilities_failed,
        'records_uploaded': run.records_uploaded,
        'message': run.message,
        'facilities': [
            {
                'id': log.pk,
                'label': log.facility_label,
                'status': log.status,
                'records_uploaded': log.records_uploaded,
                'batches_total': log.batches_total,
                'batches_completed': log.batches_completed,
                'error_message': log.error_message[:ERROR_PREVIEW_CHARS] if log.error_message else '',
            }
            for log in logs
        ],
    }


def _start_run(request, mode, dates=None, facilities=None, retry_of=None,
               is_backfill=False):
    """Create a run and hand it to a detached process, refusing to overlap.

    `dates` short-circuits form validation for retries, which reuse the original
    run's date range rather than asking the user for it again.
    """
    services.mark_stale_runs()
    running = services.active_run()
    if running:
        return JsonResponse({
            'error': 'An upload is already running (started {}). Wait for it to '
                     'finish before starting another.'.format(
                         timezone.localtime(running.created_at).strftime('%H:%M'),
                     ),
            'run_id': running.pk,
        }, status=409)

    if mode in UploadRun.MULTI_MODES:
        if facilities is None:
            facilities = list(Facility.objects.for_mode(mode).filter(is_active=True))
        if not facilities:
            return JsonResponse({'error': (
                'No active tenant databases have been discovered.'
                if mode == 'tenant' else 'No active facilities are configured.'
            )}, status=400)

    if dates is None:
        if is_backfill:
            # A backfill has no window; the stored dates just record when it ran.
            today = timezone.localdate()
            dates = (today, today)
        else:
            form = UploadForm(request.POST)
            if not form.is_valid():
                return JsonResponse({'error': form.errors.as_text()}, status=400)
            dates = (form.cleaned_data['date_from'], form.cleaned_data['date_to'])

    run = services.create_run(
        date_from=dates[0],
        date_to=dates[1],
        triggered_by='manual',
        user=request.user,
        mode=mode,
        facilities=facilities,
        retry_of=retry_of,
        is_backfill=is_backfill,
    )
    services.spawn_run(run.pk)
    return JsonResponse({'run_id': run.pk})


@login_required
def upload_view(request):
    if request.method == 'POST':
        return _start_run(request, mode='single')

    services.mark_stale_runs()
    logs = UploadLog.objects.filter(facility__isnull=True, run__mode='single')[:50]
    # Logs written before multi-facility support have no run at all.
    legacy = UploadLog.objects.filter(run__isnull=True)[:50]
    logs = sorted(
        list(logs) + list(legacy), key=lambda log: log.created_at, reverse=True,
    )[:50]
    app_settings = AppSettings.load()
    cron_mode = app_settings.cron_mode()
    return render(request, 'upload/upload.html', {
        'form': UploadForm(),
        'logs': logs,
        # "The nightly job covers more than this page does" — true of both
        # multi-facility and multi-tenant mode, and the page says the same thing
        # either way; only the noun changes.
        'multi_enabled': cron_mode != 'single',
        'cron_mode_label': 'Multi-tenant' if cron_mode == 'tenant' else 'Multi-facility',
        'settings_obj': app_settings,
    })


@login_required
def run_progress(request, run_id):
    """Poll one run's status, including every facility inside it."""
    try:
        run = UploadRun.objects.get(pk=run_id)
    except UploadRun.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    if run.mode in UploadRun.MULTI_MODES and not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    return JsonResponse(_serialize_run(run))


def _multi_facilities_context(facility_form=None, editing_pk=None):
    services.mark_stale_runs()
    # Standalone only: a discovered tenant is managed on /multi-tenant, and
    # listing a hundred of them here would bury the containers this page is for.
    facilities = Facility.objects.standalone()
    return {
        'form': UploadForm(),
        'facility_form': facility_form or FacilityForm(),
        'editing_pk': editing_pk,
        'facilities': facilities,
        'runs': UploadRun.objects.filter(mode='multi')[:50],
        'settings_obj': AppSettings.load(),
        'active_run': services.active_run(),
        'backfill_pending_count': sum(
            1 for f in facilities if f.is_active and f.initial_backfill_at is None
        ),
    }


@login_required
@staff_required
def multi_facilities(request):
    return render(request, 'upload/multi_facilities.html',
                  _multi_facilities_context())


@login_required
@staff_required
@require_POST
def facility_save(request, pk=None):
    # server__isnull: a tenant's connection belongs to its server, so this form
    # must not be able to reach one even with a hand-crafted URL.
    instance = get_object_or_404(Facility, pk=pk, server__isnull=True) if pk else None
    form = FacilityForm(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        return redirect('upload:multi_facilities')

    # Re-render with errors rather than silently dropping the submission.
    return render(request, 'upload/multi_facilities.html',
                  _multi_facilities_context(facility_form=form, editing_pk=pk),
                  status=400)


@login_required
@staff_required
@require_POST
def facility_delete(request, pk):
    facility = get_object_or_404(Facility, pk=pk, server__isnull=True)
    facility.delete()
    return redirect('upload:multi_facilities')


@login_required
@staff_required
@require_POST
def facility_test(request):
    """Connect to a facility and report the MFL an upload would send for it."""
    pk = request.POST.get('pk') or None
    password = request.POST.get('password', '')

    if not password and pk:
        # Editing without retyping: test the password already stored.
        try:
            password = get_object_or_404(Facility, pk=pk).get_password()
        except ValueError as exc:
            return JsonResponse({'ok': False, 'message': str(exc)})
    if not password:
        return JsonResponse({'ok': False, 'message': 'A MySQL password is required.'})

    config = openmrs.FacilityConfig(
        label=request.POST.get('name', ''),
        host=request.POST.get('host', ''),
        port=int(request.POST.get('port') or 3306),
        user=request.POST.get('username', ''),
        password=password,
        database=request.POST.get('database_name') or 'openmrs',
    )
    result = openmrs.probe(config)

    if result['ok'] and result['mfl']:
        clash = Facility.objects.filter(mfl_code=result['mfl'])
        if pk:
            clash = clash.exclude(pk=pk)
        other = clash.first()
        if other:
            result['ok'] = False
            result['message'] = (
                'MFL {} is already used by "{}". The DIFF platform identifies '
                'facilities by MFL, so these two would overwrite each '
                'other.'.format(result['mfl'], other.name)
            )

    if pk:
        Facility.objects.filter(pk=pk).update(
            last_tested_at=timezone.now(),
            last_test_ok=result['ok'],
            last_test_message=result['message'],
            mfl_code=result['mfl'] if result['ok'] else '',
            mfl_facility_name=result['facility_name'] if result['ok'] else '',
        )
    return JsonResponse(result)


@login_required
@staff_required
@require_POST
def multi_settings(request):
    app_settings = AppSettings.load()
    enabled = request.POST.get('multi_facility_enabled') == 'on'
    app_settings.multi_facility_enabled = enabled
    # The two modes describe incompatible deployments — separate containers
    # versus one shared server — so turning this one on turns the other off
    # rather than leaving cron_mode()'s precedence rule to decide silently.
    if enabled:
        app_settings.multi_tenant_enabled = False
    app_settings.save()
    return redirect('upload:multi_facilities')


@login_required
@staff_required
@require_POST
def multi_upload(request):
    return _start_run(request, mode='multi')


@login_required
@require_POST
def backfill_upload(request):
    """Run the one-off initial upload of every pending appointment.

    Normally the next cron job does this by itself; the button exists so a fresh
    install can be loaded immediately after setup instead of waiting for 6am.
    Re-running it once it has happened is allowed — it uploads the same records
    again, which the platform appends, but that is the operator's call to make.
    """
    mode = AppSettings.load().cron_mode()
    if mode in UploadRun.MULTI_MODES and not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)
    return _start_run(request, mode=mode, is_backfill=True)


@login_required
@staff_required
@require_POST
def run_retry_failed(request, run_id):
    """Re-upload only the facilities that failed in a previous run."""
    run = get_object_or_404(UploadRun, pk=run_id)
    facilities = [
        log.facility for log in run.logs.filter(status='failed').select_related('facility')
        if log.facility_id
    ]
    if not facilities:
        return JsonResponse(
            {'error': 'No failed facilities left to retry.'}, status=400,
        )

    return _start_run(
        request,
        # The retry belongs to the same setup as the run it repeats, so its
        # history shows up on the page the operator started from.
        mode=run.mode if run.mode in UploadRun.MULTI_MODES else 'multi',
        dates=(run.date_from, run.date_to),
        facilities=facilities,
        retry_of=run,
        # Retrying a backfill must re-run the backfill: the stored dates are the
        # day it ran, not a window worth re-querying.
        is_backfill=run.is_backfill,
    )


# --------------------------------------------------------------- multi-tenant

def _multi_tenant_context(server_form=None, editing_pk=None):
    services.mark_stale_runs()
    facilities = list(
        Facility.objects.tenants().select_related('server').order_by('server__name', 'name')
    )
    return {
        'form': UploadForm(),
        'server_form': server_form or TenantServerForm(),
        'editing_pk': editing_pk,
        'servers': TenantServer.objects.all(),
        'facilities': facilities,
        'active_count': sum(1 for f in facilities if f.is_active),
        'runs': UploadRun.objects.filter(mode='tenant')[:50],
        'settings_obj': AppSettings.load(),
        'active_run': services.active_run(),
        'backfill_pending_count': sum(
            1 for f in facilities if f.is_active and f.initial_backfill_at is None
        ),
    }


@login_required
@staff_required
def multi_tenant(request):
    return render(request, 'upload/multi_tenant.html', _multi_tenant_context())


@login_required
@staff_required
@require_POST
def tenant_settings(request):
    app_settings = AppSettings.load()
    enabled = request.POST.get('multi_tenant_enabled') == 'on'
    app_settings.multi_tenant_enabled = enabled
    if enabled:
        app_settings.multi_facility_enabled = False
    app_settings.save()
    return redirect('upload:multi_tenant')


@login_required
@staff_required
@require_POST
def tenant_server_save(request, pk=None):
    """Save a server and immediately discover what it holds.

    Saving and syncing are one action on purpose: a server with no facilities
    behind it is not a useful thing to have stored, and the whole point of this
    setup is that the operator never enumerates them by hand.
    """
    instance = get_object_or_404(TenantServer, pk=pk) if pk else None
    form = TenantServerForm(request.POST, instance=instance)
    if not form.is_valid():
        return render(request, 'upload/multi_tenant.html',
                      _multi_tenant_context(server_form=form, editing_pk=pk),
                      status=400)

    server = form.save()
    summary = tenants.sync_server(server)
    if summary['ok']:
        messages.success(request, '{}: {}'.format(server.name, summary['message']))
    else:
        messages.error(request, '{}: {}'.format(server.name, summary['message']))
    return redirect('upload:multi_tenant')


@login_required
@staff_required
@require_POST
def tenant_server_delete(request, pk):
    """Delete a server and the facilities discovered on it.

    Cascading is right here in a way it would not be for a hand-entered
    facility: nothing about a discovered row is the operator's own work, and
    the upload history survives regardless — UploadLog keeps its own label and
    lets the facility go.
    """
    server = get_object_or_404(TenantServer, pk=pk)
    server.delete()
    return redirect('upload:multi_tenant')


@login_required
@staff_required
@require_POST
def tenant_server_test(request):
    """Report which databases a server would contribute, without saving it."""
    pk = request.POST.get('pk') or None
    password = request.POST.get('password', '')

    if not password and pk:
        # Editing without retyping: test the password already stored.
        try:
            password = get_object_or_404(TenantServer, pk=pk).get_password()
        except ValueError as exc:
            return JsonResponse({'ok': False, 'message': str(exc)})
    if not password:
        return JsonResponse({'ok': False, 'message': 'A MySQL password is required.'})

    config = openmrs.FacilityConfig(
        label=request.POST.get('name', ''),
        host=request.POST.get('host', ''),
        port=int(request.POST.get('port') or 3306),
        user=request.POST.get('username', ''),
        password=password,
        database='',
    )
    result = openmrs.probe_server(
        config, request.POST.get('database_prefix', '').strip(),
    )
    # The full list can be a hundred names; the UI only needs enough to confirm
    # the prefix is selecting the right things.
    result['sample'] = result['databases'][:20]
    result['count'] = len(result['databases'])
    del result['databases']
    return JsonResponse(result)


@login_required
@staff_required
@require_POST
def tenant_server_sync(request, pk):
    """Re-read one server's database list and re-identify its schemas."""
    server = get_object_or_404(TenantServer, pk=pk)
    summary = tenants.sync_server(server)
    return JsonResponse(summary)


@login_required
@staff_required
@require_POST
def tenant_toggle(request, pk):
    """Include or exclude one discovered database in uploads.

    Clears `disabled_by_sync` either way: once an operator has made the call by
    hand, a later sync must not overturn it.
    """
    facility = get_object_or_404(Facility, pk=pk, server__isnull=False)
    facility.is_active = not facility.is_active
    facility.disabled_by_sync = False
    facility.save(update_fields=['is_active', 'disabled_by_sync', 'updated_at'])
    return redirect('upload:multi_tenant')


@login_required
@staff_required
@require_POST
def tenant_upload(request):
    return _start_run(request, mode='tenant')
