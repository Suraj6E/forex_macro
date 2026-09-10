"""Django settings for the Forex Macro Event Impact Explorer.

Single user, local, Windows.  See planning.md §8 for why this is SQLite +
Parquet rather than one database.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

# --- data tree (planning.md §8 repo layout) --------------------------------
DATA_DIR = BASE_DIR / "data"
RAW_DIR = DATA_DIR / "raw"          # immutable fetched payloads, content-hashed
PARQUET_DIR = DATA_DIR / "parquet"  # price bars, pair / year-month
EXPORT_DIR = DATA_DIR / "exports"   # dataset exports + provenance.json
for _d in (DATA_DIR, RAW_DIR, PARQUET_DIR, EXPORT_DIR):
    _d.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get(
    "FXMACRO_SECRET_KEY", "dev-only-local-single-user-not-a-secret"
)
DEBUG = os.environ.get("FXMACRO_DEBUG", "1") == "1"
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]

# The Django admin is a generic table editor behind a login. On a single-user
# local tool that login protects nothing, so it is off unless asked for; the
# screens the project needs are first-class pages instead.
ADMIN_ENABLED = os.environ.get("FXMACRO_ADMIN", "0") == "1"

INSTALLED_APPS = [
    *(["django.contrib.admin"] if ADMIN_ENABLED else []),
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "dashboard",
    "sources",
    "calendar_data",
    "prices",
    "quality",
    "studies",
    "commentary",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "dashboard.context.nav_counts",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# WAL matters here: the background worker thread (sources/jobs.py) writes
# while the console reads.  Without it a long fetch blocks every page load.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            "init_command": (
                "PRAGMA journal_mode=WAL;"
                "PRAGMA synchronous=NORMAL;"
                "PRAGMA busy_timeout=10000;"
                "PRAGMA foreign_keys=ON;"
            ),
            "transaction_mode": "IMMEDIATE",
        },
    }
}

AUTH_PASSWORD_VALIDATORS = []

# planning.md §4.1: everything is stored UTC, timezone-aware.  Display
# conversion happens in templates, never in the database.
LANGUAGE_CODE = "en-gb"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- ingestion ------------------------------------------------------------
# Set FXMACRO_WORKER=0 to run management commands without spawning the worker.
RUN_WORKER = os.environ.get("FXMACRO_WORKER", "1") == "1"
WORKER_POLL_SECONDS = 2.0

# Honest User-Agent, per planning.md §5.4 point 2.
HTTP_USER_AGENT = (
    "fxmacro/0.1 (local research tool; single user; contact: local install)"
)
HTTP_TIMEOUT_SECONDS = 30

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.db.backends": {"level": "WARNING"}},
}
