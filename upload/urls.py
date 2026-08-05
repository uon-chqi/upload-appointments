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
]
