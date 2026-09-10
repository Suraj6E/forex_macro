from django.urls import path

from prices import views

app_name = "prices"

urlpatterns = [
    path("", views.index, name="index"),
]
