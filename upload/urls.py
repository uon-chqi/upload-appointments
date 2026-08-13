from django.urls import path

from . import views

app_name = 'upload'

urlpatterns = [
    path('', views.upload_view, name='upload'),
    path('backfill/', views.backfill_upload, name='backfill_upload'),
    path('runs/<int:run_id>/progress/', views.run_progress, name='run_progress'),

    path('multi-facilities/', views.multi_facilities, name='multi_facilities'),
    path('multi-facilities/settings/', views.multi_settings, name='multi_settings'),
    path('multi-facilities/upload/', views.multi_upload, name='multi_upload'),
    path('multi-facilities/facilities/add/', views.facility_save, name='facility_add'),
    path('multi-facilities/facilities/test/', views.facility_test, name='facility_test'),
    path('multi-facilities/facilities/<int:pk>/edit/', views.facility_save, name='facility_edit'),
    path('multi-facilities/facilities/<int:pk>/delete/', views.facility_delete, name='facility_delete'),
    path('multi-facilities/runs/<int:run_id>/retry-failed/', views.run_retry_failed, name='run_retry_failed'),

    path('multi-tenant/', views.multi_tenant, name='multi_tenant'),
    path('multi-tenant/settings/', views.tenant_settings, name='tenant_settings'),
    path('multi-tenant/upload/', views.tenant_upload, name='tenant_upload'),
    path('multi-tenant/servers/add/', views.tenant_server_save, name='tenant_server_add'),
    path('multi-tenant/servers/test/', views.tenant_server_test, name='tenant_server_test'),
    path('multi-tenant/servers/<int:pk>/edit/', views.tenant_server_save, name='tenant_server_edit'),
    path('multi-tenant/servers/<int:pk>/delete/', views.tenant_server_delete, name='tenant_server_delete'),
    path('multi-tenant/servers/<int:pk>/sync/', views.tenant_server_sync, name='tenant_server_sync'),
    path('multi-tenant/databases/<int:pk>/toggle/', views.tenant_toggle, name='tenant_toggle'),
]
