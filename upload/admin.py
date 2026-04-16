from django.contrib import admin

from .models import UploadLog


@admin.register(UploadLog)
class UploadLogAdmin(admin.ModelAdmin):
    list_display = ['date_from', 'date_to', 'triggered_by', 'triggered_by_user', 'status', 'records_uploaded', 'created_at']
    list_filter = ['status', 'triggered_by', 'created_at']
    search_fields = ['error_message']
    readonly_fields = ['date_from', 'date_to', 'triggered_by', 'triggered_by_user', 'status', 'records_uploaded', 'error_message', 'created_at']
