from django.urls import path

from quality import views

app_name = "quality"

urlpatterns = [
    path("", views.index, name="index"),
]
