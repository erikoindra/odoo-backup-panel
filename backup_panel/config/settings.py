"""Django settings. Everything that differs per deployment comes from environment variables
(see docs/spec.md section 6)."""

import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from config.envfile import load_env_file

BASE_DIR = Path(__file__).resolve().parent.parent

# Optional file with KEY=VALUE lines (outside the repo). Real environment variables win over it.
if os.environ.get("PANEL_ENV_FILE"):
    load_env_file(os.environ["PANEL_ENV_FILE"])


def env_bool(name, default=False):
    return os.environ.get(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


DEBUG = env_bool("DJANGO_DEBUG")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if not DEBUG:
        raise ImproperlyConfigured("DJANGO_SECRET_KEY is required when DJANGO_DEBUG is off")
    SECRET_KEY = "dev-only-insecure-key"

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "axes",
    "panel",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "panel.middleware.HtmxLoginRedirectMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",  # last, as django-axes requires
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",  # first, so locked-out clients never reach the password check
    "django.contrib.auth.backends.ModelBackend",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("PANEL_DB_PATH", str(BASE_DIR / "panel.sqlite3")),
        "OPTIONS": {
            # WAL: the web process keeps reading while the job worker writes.
            "init_command": "PRAGMA journal_mode=WAL;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en"
TIME_ZONE = os.environ.get("TIME_ZONE", "Asia/Jakarta")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "instance_list"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", str(8 * 3600)))

# --- behind nginx (docs/decisions.md D4) ---
# Number of reverse proxies in front of the panel that append to X-Forwarded-For.
# 0 = the panel is reached directly (local dev); 1 = nginx.
TRUSTED_PROXY_COUNT = int(os.environ.get("TRUSTED_PROXY_COUNT", "0"))
if TRUSTED_PROXY_COUNT > 0:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# --- where the backup containers are (docs/spec.md section 4) ---
# Portainer: every Docker environment the API key can see becomes a server in the panel.
PORTAINER_URL = os.environ.get("PORTAINER_URL", "").split("#")[0].strip().rstrip("/")
PORTAINER_API_KEY = os.environ.get("PORTAINER_API_KEY", "")
PORTAINER_ENVIRONMENTS = env_list("PORTAINER_ENVIRONMENTS")  # optional allowlist by environment name
# Plain Docker API endpoints, e.g. a socket-proxy: "local=tcp://socket-proxy:2375,other=tcp://..."
DOCKER_ENDPOINTS = env_list("DOCKER_ENDPOINTS")
BACKUP_IMAGE_PREFIX = os.environ.get("BACKUP_IMAGE_PREFIX", "ghcr.io/erikoindra/odoo-backup-service")

# Backup and restore run in the worker (manage.py run_jobs). A command that is still running after
# JOB_TIMEOUT seconds is given up on (it keeps running in its container; only the job is marked failed).
JOB_TIMEOUT = int(os.environ.get("JOB_TIMEOUT", "3600"))
JOB_POLL_INTERVAL = float(os.environ.get("JOB_POLL_INTERVAL", "1.0"))  # seconds between status checks

# Secure cookies need HTTPS; on by default whenever DEBUG is off.
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool("DJANGO_SECURE_COOKIES", not DEBUG)
X_FRAME_OPTIONS = "DENY"
# Behind nginx the panel is only reached over https, so send plain-http requests to https. nginx does the
# same first; this is the second line. HSTS stays off until https is known to work (a browser keeps it).
SECURE_SSL_REDIRECT = env_bool("DJANGO_SSL_REDIRECT", TRUSTED_PROXY_COUNT > 0 and not DEBUG)
SECURE_HSTS_SECONDS = int(os.environ.get("DJANGO_HSTS_SECONDS", "0"))
SECURE_REFERRER_POLICY = "same-origin"
# Two HSTS options that are decisions about the whole domain, not about this panel: includeSubdomains
# would also force https on every other subdomain, and preload cannot be taken back. Neither is switched on
# for you, so their reminders are silenced. W004 (HSTS off) is kept on purpose.
SILENCED_SYSTEM_CHECKS = ["security.W005", "security.W021"]

# --- django-axes: lock a client IP after repeated failed logins ---
AXES_FAILURE_LIMIT = int(os.environ.get("AXES_FAILURE_LIMIT", "5"))
AXES_COOLOFF_TIME = 1  # hours; the axes default (None) would lock forever
AXES_LOCKOUT_PARAMETERS = ["ip_address"]
AXES_CLIENT_IP_CALLABLE = "panel.ip.client_ip"  # real client IP behind nginx, not nginx's IP
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "registration/locked.html"
