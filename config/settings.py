"""
Django settings for config project (Phase 1: foundation, auth, RBAC).
"""

import sys
from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(
    DEBUG=(bool, False),
)
# overwrite=True: when a local .env file exists, its values win over
# whatever's already sitting in the shell's own environment. Without this,
# django-environ only fills in variables that are *absent* from os.environ —
# if a terminal/IDE run config has ever exported e.g. OPENAI_API_KEY="" (even
# empty), that silently wins over .env's real value with zero error or
# warning, which is exactly what caused every AI reply to fail while the
# key worked fine from every other shell. In production there's no .env
# file at all, so this is a no-op there — real platform env vars still win.
environ.Env.read_env(BASE_DIR / ".env", overwrite=True)

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
# Needed explicitly for POSTs to work once real domains are behind a
# reverse proxy (Cloudflare, Nginx) — Django's CSRF check compares the
# request's Origin/Referer against this list, and ALLOWED_HOSTS alone
# doesn't satisfy it. Must be full origins (scheme + host), not bare
# hostnames like ALLOWED_HOSTS.
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

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
    "axes",
    "django_celery_beat",
    "accounts",
    "chat",
    "governance",
    "notifications",
    "providers",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Before LocaleMiddleware so its IP-based guess (for anonymous, first-
    # time visitors only) is in place before LocaleMiddleware reads the
    # language cookie - see accounts/middleware.py.
    "accounts.middleware.GeoLanguageMiddleware",
    # Must sit after SessionMiddleware and before CommonMiddleware - Django's
    # own hard requirement, not just a convention (see LocaleMiddleware docs).
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # After AuthenticationMiddleware (needs request.user) and LocaleMiddleware
    # (this overrides its guess with the logged-in user's stored preference -
    # see accounts/middleware.py for why that's not the same as session-only
    # persistence).
    "accounts.middleware.UserLanguagePreferenceMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "axes.middleware.AxesMiddleware",  # must stay last (see django-axes docs)
]

AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",  # must be first - checks lockout before real auth
    "django.contrib.auth.backends.ModelBackend",
]

# Login brute-force protection (django-axes) - tracked in the DB, no Redis
# needed. Locks the ACCOUNT after AXES_FAILURE_LIMIT failures within
# AXES_COOLOFF_TIME, per spec: 5 attempts / 15-30 min cooldown. A
# successful login always resets the counter.
AXES_FAILURE_LIMIT = 5
AXES_COOLOFF_TIME = timedelta(minutes=20)
# Deliberately username-only, NOT ip_address: this app is used by many
# employees from behind the same shared office IP/NAT. Locking by IP too
# (django-axes' own recommendation, its W006 check) would mean one
# coworker mistyping their password 5 times locks out the entire office
# for 20 minutes - a worse outcome than the brute-force risk it prevents.
# Cross-account-same-IP attack *patterns* are still fully visible to admins
# via the audit log (every lockout logs its IP - see accounts/signals.py),
# which is the spec's own stated detection mechanism for that case.
AXES_LOCKOUT_PARAMETERS = ["username"]
# Our login form is Django's standard AuthenticationForm, which always
# posts the field as "username" even though its value is an email address
# (USERNAME_FIELD="email" only affects authenticate(), not the form's
# field name) - without this, axes looks for a POST field called "email"
# that doesn't exist, silently failing to resolve who it just locked out.
AXES_USERNAME_FORM_FIELD = "username"
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_CALLABLE = "accounts.axes_hooks.axes_lockout_response"
SILENCED_SYSTEM_CHECKS = ["axes.W006"]  # the ip_address-lockout tradeoff above is deliberate

# AxesStandaloneBackend requires a real `request` object passed to
# authenticate() - Django's own test Client.login() shortcut doesn't pass
# one (a known django-axes/test-client incompatibility), which would break
# every existing test that uses self.client.login(...) instead of
# force_login(). Same test-only-behavior-change pattern as PASSWORD_HASHERS
# below: brute-force protection isn't what the test suite is exercising,
# and tests aren't the actual attack surface.
AXES_ENABLED = "test" not in sys.argv

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


# Database backups (see accounts/management/commands/backup_database.py and
# docs/BACKUP_RESTORE.md). All blank by default so the command fails loudly
# with a clear message instead of silently no-op'ing if run before it's
# configured. 30-day retention by default (spec's stated 14-30 day range,
# upper end - a few dozen compressed dumps is cheap; lower with
# BACKUP_RETENTION_DAYS if storage cost matters more than history depth).
BACKUP_S3_BUCKET = env("BACKUP_S3_BUCKET", default="")
BACKUP_S3_ENDPOINT_URL = env("BACKUP_S3_ENDPOINT_URL", default="")
BACKUP_S3_ACCESS_KEY_ID = env("BACKUP_S3_ACCESS_KEY_ID", default="")
BACKUP_S3_SECRET_ACCESS_KEY = env("BACKUP_S3_SECRET_ACCESS_KEY", default="")
BACKUP_S3_REGION = env("BACKUP_S3_REGION", default="")
BACKUP_RETENTION_DAYS = env.int("BACKUP_RETENTION_DAYS", default=30)


# Celery (background jobs: notification emails, daily plan-expiry sweep).
# REDIS_URL blank (local dev, no Redis running) -> tasks execute
# synchronously in-process instead of being queued (CELERY_TASK_ALWAYS_EAGER)
# so `.delay()` calls still work correctly without a broker/worker - the
# same escape hatch pattern used elsewhere in this file for test-only
# behavior. Set a real REDIS_URL in production to actually queue tasks.
REDIS_URL = env("REDIS_URL", default="")
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = REDIS_URL or None
CELERY_TASK_ALWAYS_EAGER = not REDIS_URL
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"
# Periodic tasks (e.g. the daily plan-expiry sweep) are configured through
# the admin-editable django-celery-beat tables, not hardcoded here - see
# notifications/migrations for the seeded schedule.
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"

# Response cache (chat/response_cache.py) — same Redis instance as Celery,
# namespaced with KEY_PREFIX so its keys never collide with Celery's own.
# Falls back to Django's in-process LocMemCache when REDIS_URL is unset
# (same "safe no-op locally, real behavior once configured" pattern as
# Celery/email/Sentry/backups above) - caching still works within one dev
# server process, it just isn't shared across workers/restarts.
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "portal_cache",
        }
    }
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}


# AI provider credentials — set these in .env, never commit real values.
# Superseded by the providers app's own DB-stored, per-provider encrypted
# keys (see providers/models.py) — kept as a fallback only until every
# environment has been migrated onto a connected Provider row.
OPENAI_API_KEY = env("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY", default="")

# Symmetric key (Fernet, urlsafe-base64, 32 bytes) encrypting Provider.
# api_key_encrypted at rest - generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Same fail-loudly-in-production pattern as SECRET_KEY above: a throwaway
# dev-only default is fine locally, but DEBUG=False must never silently run
# on it - anyone who could read settings.py would be able to decrypt every
# stored provider API key.
_INSECURE_DEFAULT_FIELD_ENCRYPTION_KEY = "m-BXX2G5tlXaL4hriTr1BwFRTtI0n1Y3i_k_Q-8yDyc="
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", default=_INSECURE_DEFAULT_FIELD_ENCRYPTION_KEY)
if not DEBUG and FIELD_ENCRYPTION_KEY == _INSECURE_DEFAULT_FIELD_ENCRYPTION_KEY and "test" not in sys.argv:
    raise ImproperlyConfigured(
        "FIELD_ENCRYPTION_KEY is not set. Generate a real one (see comment above) and set it in the "
        "environment before running with DEBUG=False."
    )


# Custom user model
AUTH_USER_MODEL = "accounts.User"

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# Session hardening. HttpOnly/SameSite=Lax are Django's defaults already,
# made explicit here so they're not silently relying on defaults changing
# out from under this app. SESSION_COOKIE_AGE + SESSION_SAVE_EVERY_REQUEST
# together give an effective 12-hour *idle* timeout (each request pushes
# expiry forward) rather than an indefinite session or a fixed absolute
# expiry that's disconnected from actual activity. SESSION_COOKIE_SECURE
# is set below only when DEBUG=False (see production hardening block) —
# forcing it here would break plain-HTTP local dev.
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 12
SESSION_SAVE_EVERY_REQUEST = True
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"


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

# UI label translation only (buttons/menus/headings) - never the AI's own
# conversation content, which already follows the user's language naturally
# per the base system prompt (see chat/prompts.py).
LANGUAGES = [
    ("en", "English"),
    ("ur", "اردو"),
    ("ar", "العربية"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_TZ = True


# Static files (CSS, JavaScript, Images)
STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Django 5.x reads storage backends from STORAGES, not the legacy
# STATICFILES_STORAGE setting (which silently no-ops here) - this is what
# actually switches static file storage to WhiteNoise's compressing,
# cache-busting backend in production.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "whitenoise.storage.CompressedManifestStaticFilesStorage"
            if not DEBUG
            else "django.contrib.staticfiles.storage.StaticFilesStorage"
        ),
    },
}

# User-uploaded chat attachments. NOTE: on Railway/most PaaS this is local
# container disk, not persistent storage — files won't survive a redeploy
# or restart. Fine for local dev/demo; swap for real object storage
# (S3-compatible) before relying on this in production.
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# Fallback file-upload limits, used whenever a user/department has no
# UsageLimit row (or that row leaves the field blank) overriding them.
DEFAULT_MAX_UPLOAD_SIZE_MB = env.int("DEFAULT_MAX_UPLOAD_SIZE_MB", default=10)
DEFAULT_ALLOWED_FILE_EXTENSIONS = env(
    "DEFAULT_ALLOWED_FILE_EXTENSIONS",
    default="pdf,txt,csv,md,png,jpg,jpeg,docx,xlsx,json",
)

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Production hardening. Cloudflare/Nginx terminate TLS in front of this app
# (see deployment/), so these only bite once DEBUG=False in a real deploy —
# they'd break plain-HTTP local dev otherwise.
#
# FORCE_HTTPS defaults True (the real production posture) but is overridable
# for a transitional bare-IP deploy that has no domain/cert yet, where
# nothing listens on 443 at all. All of SECURE_SSL_REDIRECT/cookie-Secure/HSTS
# are tied to the SAME flag deliberately: turning off just the redirect while
# leaving Secure-flagged cookies on would make browsers silently refuse to
# send/store the session or CSRF cookie over plain HTTP - login would look
# like it works (the POST succeeds) but never actually stick.
if not DEBUG:
    FORCE_HTTPS = env.bool("FORCE_HTTPS", default=True)
    SECURE_SSL_REDIRECT = FORCE_HTTPS
    SESSION_COOKIE_SECURE = FORCE_HTTPS
    CSRF_COOKIE_SECURE = FORCE_HTTPS
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000) if FORCE_HTTPS else 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = FORCE_HTTPS
    SECURE_HSTS_PRELOAD = FORCE_HTTPS
    # Nginx sits between Cloudflare and Gunicorn and sets this per deployment/nginx.conf.example.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# Always log real exceptions to the console, independent of Sentry — a
# blank SENTRY_DSN previously meant capture_exception() was a silent no-op,
# so a failing AI provider call left zero trace anywhere and required live
# debugging to diagnose. This guarantees a traceback lands somewhere even
# with no Sentry DSN configured; Sentry (below) is additional, not a
# replacement for this.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "verbose"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "app.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
}
(BASE_DIR / "logs").mkdir(exist_ok=True)

# Notification emails. Without real SMTP configured, emails print to the
# console/log instead of failing or hanging — same "safe no-op until
# configured" pattern as Sentry/backups below, not a silent data loss:
# the in-app Notification row is always created either way.
EMAIL_HOST = env("EMAIL_HOST", default="")
if EMAIL_HOST:
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_PORT = env.int("EMAIL_PORT", default=587)
    EMAIL_HOST_USER = env("EMAIL_HOST_USER", default="")
    EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default="")
    EMAIL_USE_TLS = env.bool("EMAIL_USE_TLS", default=True)
    DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default=EMAIL_HOST_USER)
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
    DEFAULT_FROM_EMAIL = "noreply@example.com"
    # The console backend writes straight to sys.stdout using whatever
    # encoding the terminal defaults to - on Windows that's often cp1252,
    # which can't represent an em-dash, an arrow, or (this app explicitly
    # supports Urdu/mixed-language content) non-Latin text at all. Without
    # this, ANY non-ASCII character anywhere in a notification body turns
    # into an unhandled 500 the first time it's hit locally - a real bug
    # found by testing this exact path, not a hypothetical one.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass  # non-interactive/redirected stdout in some environments doesn't support reconfigure

# Error monitoring — only active once SENTRY_DSN is set in .env. Silent no-op otherwise.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN and "test" not in sys.argv:
    import sentry_sdk
    from sentry_sdk.integrations.django import DjangoIntegration

    def _sentry_before_send(event, hint):
        # Belt-and-suspenders on top of send_default_pii=False and
        # include_local_variables=False below: never forward the raw
        # request body (chat message content lives there) even if a
        # future SDK/integration change starts attaching it by default.
        request = event.get("request")
        if request and "data" in request:
            del request["data"]
        return event

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        integrations=[DjangoIntegration()],
        environment=env("SENTRY_ENVIRONMENT", default="development"),
        traces_sample_rate=env.float("SENTRY_TRACES_SAMPLE_RATE", default=0.0),
        # PII/data-scrubbing, tightened beyond the SDK defaults:
        send_default_pii=False,  # never attach request user/IP/cookies
        # Default is True — without this, a stack trace through post_message()
        # or stream_message() would capture the *values* of local variables
        # like `content`/`uploaded_file`, i.e. the user's actual chat text and
        # attachments, and any local holding an API key string. The built-in
        # key-name scrubber (DEFAULT_DENYLIST) wouldn't catch these because
        # they're not named like secrets - so this is disabled outright rather
        # than relied on.
        include_local_variables=False,
        max_request_body_size="never",  # extra guard alongside send_default_pii=False
        before_send=_sentry_before_send,
    )
