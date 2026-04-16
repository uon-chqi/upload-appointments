from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

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
            log = run_upload(date_from, date_to, triggered_by='manual', user=request.user)
            if log.status == 'success':
                messages.success(
                    request,
                    f"Upload successful — {log.records_uploaded} records uploaded.",
                )
            else:
                messages.error(request, f"Upload failed: {log.error_message[:300]}")
            return redirect('upload:upload')

    logs = UploadLog.objects.all()[:50]
    return render(request, 'upload/upload.html', {'form': form, 'logs': logs})
