from django.urls import path

from studies import views

app_name = "studies"

urlpatterns = [
    path("", views.index, name="index"),
    path("event-study/", views.event_study, name="event_study"),
    path("event-study/run/", views.run, name="run"),
]
