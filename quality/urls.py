from django.urls import path

from quality import views

app_name = "quality"

urlpatterns = [
    path("", views.index, name="index"),
    path("timestamps/", views.timestamps, name="timestamps"),
    path("timestamps/run/", views.run_timestamps, name="run_timestamps"),
    path("duplicates/", views.duplicates, name="duplicates"),
    path("duplicates/merge/", views.merge_indicator_group, name="merge_group"),
    path("duplicates/merge-all-exact/", views.merge_all_exact, name="merge_all_exact"),
    path("duplicates/merge-provisional/", views.merge_provisional, name="merge_provisional"),
    path("purge-orphans/", views.purge_orphans, name="purge_orphans"),
    path("purge-source/<slug:key>/", views.purge_source, name="purge_source"),
]
