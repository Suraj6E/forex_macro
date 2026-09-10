from django.urls import path

from calendar_data import views

app_name = "calendar_data"

urlpatterns = [
    path("calendar/", views.browser, name="browser"),
    path("calendar/<int:pk>/", views.event, name="event"),
    path("indicators/", views.indicators, name="indicators"),
    path("indicators/<int:pk>/", views.indicator_detail, name="indicator"),
    path("indicators/<int:pk>/update/", views.indicator_update, name="indicator_update"),
]
