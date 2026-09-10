from django.conf import settings
from django.urls import include, path

urlpatterns = [
    path("", include("dashboard.urls")),
    path("", include("calendar_data.urls")),  # /calendar/, /indicators/
    path("sources/", include("sources.urls")),
    path("quality/", include("quality.urls")),
    path("instruments/", include("prices.urls")),
    path("studies/", include("studies.urls")),
]

# Django's admin is off by default. It is a generic database editor that needs
# a username and password, which protects nothing on a single-user local tool
# and mostly gets in the way. Every screen the project actually needs — the
# calendar, the indicator mapping, the ingestion console — is a first-class
# page above. Set FXMACRO_ADMIN=1 if you ever want the raw table editor, then
# create a login with `manage.py createsuperuser`.
if settings.ADMIN_ENABLED:
    from django.contrib import admin

    urlpatterns.append(path("admin/", admin.site.urls))
