from django.urls import path

from studies import views

app_name = "studies"

urlpatterns = [
    path("", views.index, name="index"),
]
