from django.urls import path

from sources import views

app_name = "sources"

urlpatterns = [
    path("", views.console, name="console"),
    path("fetch/<slug:key>/", views.fetch, name="fetch"),
    path("jobs/panel/", views.jobs_panel, name="jobs_panel"),
    path("jobs/<int:pk>/", views.job_detail, name="job_detail"),
    path("runs/<int:pk>/", views.run_detail, name="run_detail"),
    path("unmapped/", views.unmapped, name="unmapped"),
]
