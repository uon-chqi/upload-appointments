import threading

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render

from .forms import UploadForm
from .models import UploadLog
from .services import run_upload


@login_required
def upload_view(request):
    form = UploadForm()

    if request.method == 'POST':
        form = UploadForm(request.POST)
        if form.is_valid():
            date_from = form.cleaned_data['date_from']
            date_to = form.cleaned_data['date_to']

            # Create a pending log and kick off upload in background
            log = UploadLog.objects.create(
                date_from=date_from,
                date_to=date_to,
                triggered_by='manual',
                triggered_by_user=request.user,
                status='pending',
            )
            thread = threading.Thread(
                target=_run_upload_thread,
                args=(log.pk, date_from, date_to, request.user.pk),
                daemon=True,
            )
            thread.start()

            return JsonResponse({'log_id': log.pk})

    logs = UploadLog.objects.all()[:50]
    return render(request, 'upload/upload.html', {'form': form, 'logs': logs})


def _run_upload_thread(log_pk, date_from, date_to, user_pk):
    """Run the upload in a background thread, reusing the pre-created log."""
    from django.contrib.auth.models import User
    from django.db import connections

    try:
        user = User.objects.get(pk=user_pk)
        log = UploadLog.objects.get(pk=log_pk)
        run_upload(date_from, date_to, triggered_by='manual', user=user, log=log)
    finally:
        connections.close_all()


@login_required
def upload_progress(request, log_id):
    """Return JSON with upload progress for polling."""
    try:
        log = UploadLog.objects.get(pk=log_id)
    except UploadLog.DoesNotExist:
        return JsonResponse({'error': 'Not found'}, status=404)

    return JsonResponse({
        'status': log.status,
        'records_uploaded': log.records_uploaded,
        'batches_total': log.batches_total,
        'batches_completed': log.batches_completed,
        'error_message': log.error_message[:300] if log.error_message else '',
    })
