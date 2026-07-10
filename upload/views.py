from django.contrib.auth.decorators import login_required, user_passes_test
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import openmrs, services
from .forms import FacilityForm, UploadForm
from .models import AppSettings, Facility, UploadLog, UploadRun

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


def _start_run(request, mode, dates=None, facilities=None, retry_of=None):
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

    if mode == 'multi':
        if facilities is None:
            facilities = list(Facility.objects.filter(is_active=True))
        if not facilities:
            return JsonResponse(
                {'error': 'No active facilities are configured.'}, status=400,
            )

    if dates is None:
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
    return render(request, 'upload/upload.html', {
        'form': UploadForm(),
        'logs': logs,
        'multi_enabled': AppSettings.load().multi_facility_enabled,
    })


@login_required
def run_progress(request, run_id):
    """Poll one run's status, including every facility inside it."""
    try:
        run = UploadRun.objects.get(pk=run_id)
    except UploadRun.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    if run.mode == 'multi' and not request.user.is_staff:
        return JsonResponse({'error': 'Forbidden'}, status=403)

    return JsonResponse(_serialize_run(run))


@login_required
@staff_required
def multi_facilities(request):
    services.mark_stale_runs()
    return render(request, 'upload/multi_facilities.html', {
        'form': UploadForm(),
        'facility_form': FacilityForm(),
        'facilities': Facility.objects.all(),
        'runs': UploadRun.objects.filter(mode='multi')[:50],
        'settings_obj': AppSettings.load(),
        'active_run': services.active_run(),
    })


@login_required
@staff_required
@require_POST
def facility_save(request, pk=None):
    instance = get_object_or_404(Facility, pk=pk) if pk else None
    form = FacilityForm(request.POST, instance=instance)
    if form.is_valid():
        form.save()
        return redirect('upload:multi_facilities')

    # Re-render with errors rather than silently dropping the submission.
    services.mark_stale_runs()
    return render(request, 'upload/multi_facilities.html', {
        'form': UploadForm(),
        'facility_form': form,
        'editing_pk': pk,
        'facilities': Facility.objects.all(),
        'runs': UploadRun.objects.filter(mode='multi')[:50],
        'settings_obj': AppSettings.load(),
        'active_run': services.active_run(),
    }, status=400)


@login_required
@staff_required
@require_POST
def facility_delete(request, pk):
    facility = get_object_or_404(Facility, pk=pk)
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
    app_settings.multi_facility_enabled = request.POST.get('multi_facility_enabled') == 'on'
    app_settings.save()
    return redirect('upload:multi_facilities')


@login_required
@staff_required
@require_POST
def multi_upload(request):
    return _start_run(request, mode='multi')


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
        mode='multi',
        dates=(run.date_from, run.date_to),
        facilities=facilities,
        retry_of=run,
    )
