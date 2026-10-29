from django.contrib.auth import views as auth_views
from django.urls import path
from django.views.generic import RedirectView

from panel import views

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="instance_list", permanent=False)),
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),  # POST only (Django 5)
    path("instances/", views.instance_list, name="instance_list"),
    path("e/<str:endpoint>/<str:slug>/", views.instance_detail, name="instance_detail"),
    # Fragments, loaded by the pages above with HTMX: reading /backups takes a few seconds.
    path("e/<str:endpoint>/<str:slug>/backups/", views.backups_partial, name="backups_partial"),
    path("e/<str:endpoint>/<str:slug>/summary/", views.summary_partial, name="summary_partial"),
    path("e/<str:endpoint>/<str:slug>/backups/<str:filename>/download", views.download, name="download"),
    path("e/<str:endpoint>/<str:slug>/backup", views.backup_now, name="backup_now"),
    path("e/<str:endpoint>/<str:slug>/restore/", views.restore, name="restore"),
    path("e/<str:endpoint>/<str:slug>/restore/check/", views.restore_check, name="restore_check"),
    path("jobs/", views.job_list, name="job_list"),
    path("jobs/<int:pk>/", views.job_status, name="job_status"),
]
