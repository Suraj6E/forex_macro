from django.urls import path

from sources import views

app_name = "sources"

urlpatterns = [
    path("", views.console, name="console"),
    path("seed/", views.seed, name="seed"),
    path("s/<slug:key>/", views.source_detail, name="detail"),
    path("fetch/<slug:key>/", views.fetch, name="fetch"),
    path("reparse/<slug:key>/", views.reparse, name="reparse"),
    path("toggle/<slug:key>/", views.toggle_source, name="toggle"),
    path("jobs/", views.jobs_list, name="jobs"),
    path("jobs/panel/", views.jobs_panel, name="jobs_panel"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("jobs/<int:pk>/rerun/", views.rerun_job, name="rerun_job"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("snapshots/<int:pk>/", views.snapshot_raw, name="snapshot"),
]
