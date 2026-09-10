from django.contrib import admin
from django.shortcuts import redirect
from django.urls import include, path

urlpatterns = [
    path("", lambda r: redirect("sources:console"), name="home"),
    path("sources/", include("sources.urls", namespace="sources")),
    path("admin/", admin.site.urls),
]
