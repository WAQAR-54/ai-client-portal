"""
Django settings for config project (Phase 1: foundation, auth, RBAC).
"""

import sys
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
environ.Env.read_env(BASE_DIR / ".env")

_INSECURE_DEFAULT_KEY = "django-insecure-dev-only-change-me"
SECRET_KEY = env("SECRET_KEY", default=_INSECURE_DEFAULT_KEY)
DEBUG = env.bool("DEBUG", default=True)
ALLOWED_HOSTS = env.list(
    "ALLOWED_HOSTS",
    # Leading-dot entries match any subdomain (Django convention) — safe
    # defaults for a first deploy on Railway/Render before a custom domain
    # is wired up. Set ALLOWED_HOSTS explicitly once you have a real domain.
    default=["localhost", "127.0.0.1", ".railway.app", ".onrender.com"],
)

# Fail loudly instead of silently running production on a throwaway dev key —
# DEBUG=False is our signal that this is a real deployment, not local dev.
if not DEBUG and SECRET_KEY == _INSECURE_DEFAULT_KEY and "test" not in sys.argv:
    raise ImproperlyConfigured(
        "SECRET_KEY is not set. Generate a real one and set it in the environment before running with DEBUG=False."
    )


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "accounts",
    "chat",
    "governance",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Database
# Defaults to SQLite for local dev when DATABASE_URL is not set.
# Set DATABASE_URL=postgres://user:pass@host:5432/dbname to switch to PostgreSQL.

DATABASES = {
    "default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
}


# AI provider credentials — set these in .env, never commit real values.
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")


# Custom user model
AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"


# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# The default PBKDF2 hasher is deliberately slow; swap in a fast one under
# `manage.py test` only, so the real app keeps strong hashing in dev/prod.
if "test" in sys.argv:
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


# Internationalization
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
if not DEBUG:
    STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Production hardening. Cloudflare/Nginx terminate TLS in front of this app
# (see deployment/), so these only bite once DEBUG=False in a real deploy —
# they'd break plain-HTTP local dev otherwise.
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # Nginx sits between Cloudflare and Gunicorn and sets this per deployment/nginx.conf.example.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# Error monitoring — only active once SENTRY_DSN is set in .env. Silent no-op otherwise.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN and "test" not in sys.argv:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default="development"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        send_default_pii=False,
    )
