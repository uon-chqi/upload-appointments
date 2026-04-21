from django.urls import path

from . import views

app_name = 'upload'

urlpatterns = [
    path('', views.upload_view, name='upload'),
    path('progress/<int:log_id>/', views.upload_progress, name='upload_progress'),
]
