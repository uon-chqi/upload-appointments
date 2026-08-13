from django.contrib import admin

from .models import AppSettings, Facility, TenantServer, UploadLog, UploadRun


@admin.register(AppSettings)
class AppSettingsAdmin(admin.ModelAdmin):
    list_display = ['multi_facility_enabled', 'multi_tenant_enabled', 'updated_at']

    def has_add_permission(self, request):
        return not AppSettings.objects.exists()

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(TenantServer)
class TenantServerAdmin(admin.ModelAdmin):
    list_display = ['name', 'host', 'port', 'database_prefix', 'is_active',
                    'last_sync_ok', 'last_synced_at']
    list_filter = ['is_active', 'last_sync_ok']
    search_fields = ['name', 'host']
    # password_encrypted is deliberately absent: nothing should render it.
    fields = ['name', 'host', 'port', 'username', 'database_prefix', 'is_active',
              'last_synced_at', 'last_sync_ok', 'last_sync_message']
    readonly_fields = ['last_synced_at', 'last_sync_ok', 'last_sync_message']


@admin.register(Facility)
class FacilityAdmin(admin.ModelAdmin):
    list_display = ['name', 'server', 'host', 'port', 'database_name', 'mfl_code',
                    'is_active', 'last_test_ok']
    list_filter = ['is_active', 'last_test_ok', 'server']
    search_fields = ['name', 'host', 'mfl_code', 'database_name']
    # password_encrypted is deliberately absent: nothing should render it.
    fields = ['name', 'server', 'host', 'port', 'database_name', 'username', 'is_active',
              'mfl_code', 'mfl_facility_name', 'disabled_by_sync', 'last_seen_at',
              'last_tested_at', 'last_test_ok', 'last_test_message']
    # Discovered rows are sync's to write; editing them here would be undone.
    readonly_fields = ['server', 'mfl_code', 'mfl_facility_name', 'disabled_by_sync',
                       'last_seen_at', 'last_tested_at', 'last_test_ok',
                       'last_test_message']


class UploadLogInline(admin.TabularInline):
    model = UploadLog
    extra = 0
    fields = ['facility_label', 'status', 'records_uploaded', 'batches_completed',
              'batches_total', 'finished_at']
    readonly_fields = fields
    can_delete = False


@admin.register(UploadRun)
class UploadRunAdmin(admin.ModelAdmin):
    list_display = ['date_from', 'date_to', 'mode', 'triggered_by', 'status',
                    'facilities_completed', 'facilities_total', 'facilities_failed',
                    'records_uploaded', 'created_at']
    list_filter = ['status', 'mode', 'triggered_by', 'created_at']
    inlines = [UploadLogInline]


@admin.register(UploadLog)
class UploadLogAdmin(admin.ModelAdmin):
    list_display = ['date_from', 'date_to', 'facility_label', 'triggered_by',
                    'triggered_by_user', 'status', 'records_uploaded', 'created_at']
    list_filter = ['status', 'triggered_by', 'created_at']
    search_fields = ['error_message', 'facility_label']
    readonly_fields = ['run', 'facility', 'facility_label', 'date_from', 'date_to',
                       'triggered_by', 'triggered_by_user', 'status', 'records_uploaded',
                       'batches_total', 'batches_completed', 'error_message',
                       'started_at', 'finished_at', 'created_at']
