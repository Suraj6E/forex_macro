from django.urls import path

from prices import views

app_name = "prices"

urlpatterns = [
    path("", views.index, name="index"),
    path("chart/", views.chart, name="chart"),
    path("api/candles/", views.candles_api, name="candles_api"),
]
