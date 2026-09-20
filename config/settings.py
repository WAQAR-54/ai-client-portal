"""
Django settings for config project (Phase 1: foundation, auth, RBAC).
"""

import sys
from datetime import timedelta
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

from config.db_options import postgres_timeout_options

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
# Fails CLOSED (secure) when DEBUG is absent from the environment entirely -
# matches the schema default already declared above (env = environ.Env(
# DEBUG=(bool, False))), which this used to silently override with
# default=True. A production host whose env/.env was ever bootstrapped
# without explicitly setting DEBUG would otherwise run with full tracebacks
# (SQL, file paths, potentially secrets) exposed to any visitor who
# triggers a 500 - local dev's own .env.example sets DEBUG=True explicitly,
# so this default is never actually needed there either.
DEBUG = env.bool("DEBUG", default=False)
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

# Explicit production/development switch, independent of DEBUG itself -
# unlike SECRET_KEY, a real deploy's DEBUG/SECRET_KEY values alone aren't a
# reliable enough signal here (a local .env can easily carry a non-default
# placeholder SECRET_KEY too). docker-compose.yml/the server's own .env sets
# ENVIRONMENT=production explicitly; everything else (including CI, which
# runs with DEBUG=True on purpose - see ci.yml) defaults to "development".
ENVIRONMENT = env("ENVIRONMENT", default="development")
if ENVIRONMENT == "production" and DEBUG and "test" not in sys.argv:
    raise ImproperlyConfigured(
        "DEBUG=True with ENVIRONMENT=production - refusing to start. DEBUG=True hands any visitor who triggers "
        "an error a full traceback (source paths, SQL, settings). Set DEBUG=False before deploying."
    )


# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "axes",
    "django_celery_beat",
    "accounts",
    "billing",
    "chat",
    "domaingen",
    "governance",
    "notifications",
    "playground",
    "providers",
]

MIDDLEWARE = [
    # First, so every subsequent middleware/view/log line for this request
    # can be tagged with request.id - see accounts/middleware.py.
    "accounts.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    # Right after SecurityMiddleware: redirect http visitors (per Cloudflare's CF-Visitor) before anything else runs.
    "accounts.middleware.CloudflareHttpsMiddleware",
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
    # After MessageMiddleware (uses django.contrib.messages to explain the
    # logged-out-for-inactivity redirect) and AuthenticationMiddleware
    # (needs request.user, set up above).
    "accounts.middleware.SessionTimeoutMiddleware",
    # Right after it: a browser whose account signed in elsewhere is signed out (accounts/single_session.py).
    "accounts.middleware.SingleSessionMiddleware",
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
                "governance.context_processors.branding",
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
# Opt-in session timeouts for PostgreSQL only (both 0 = off = no change; see config/db_options.py).
_db_timeouts = postgres_timeout_options(
    env.int("DB_STATEMENT_TIMEOUT_MS", default=0), env.int("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", default=0)
)
if _db_timeouts and DATABASES["default"]["ENGINE"].endswith("postgresql"):
    DATABASES["default"].setdefault("OPTIONS", {})["options"] = _db_timeouts


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
# The git commit this process was built from - set by the deploy step through
# docker-compose.yml. "" locally. Read by `manage.py ops_verify`.
RELEASE_SHA = env("RELEASE_SHA", default="")
CELERY_BROKER_URL = REDIS_URL or "memory://"
CELERY_RESULT_BACKEND = REDIS_URL or None
CELERY_TASK_ALWAYS_EAGER = not REDIS_URL
CELERY_TASK_EAGER_PROPAGATES = True
# A task that hangs (a stuck SMTP/S3/pg_dump call) used to hold a worker process forever, with no
# time limit at all. Soft limit raises SoftTimeLimitExceeded inside the task (so it can log and
# stop); the hard limit kills it. Generous on purpose - the nightly database backup is the longest
# task - and both can be raised with an environment variable.
CELERY_TASK_TIME_LIMIT = env.int("CELERY_TASK_TIME_LIMIT", default=1800)
CELERY_TASK_SOFT_TIME_LIMIT = env.int("CELERY_TASK_SOFT_TIME_LIMIT", default=1500)
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True
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

# "Sign in with Google" (accounts/google_auth.py) - the OAuth Client ID
# from a Google Cloud Console "OAuth client" of type Web application
# (Credentials > Create Credentials > OAuth client ID). No client secret
# is needed: Google Identity Services' button flow hands the browser a
# signed ID token directly, which the backend verifies against Google's
# own public keys - there's no server-to-server token exchange here.
# Blank (the default) means the button never renders, regardless of the
# SecuritySettings.google_signin_enabled toggle - see
# accounts.google_auth.google_signin_enabled().
GOOGLE_OAUTH_CLIENT_ID = env("GOOGLE_OAUTH_CLIENT_ID", default="")

LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

# base.html renders each Django message as class="alert alert-{{ message.tags }}"
# - Django's own default tag for messages.error() is "error", but this
# app's existing CSS class (predating this) is .alert-danger, so map it
# rather than adding a redundant third class.
from django.contrib.messages import constants as _message_constants  # noqa: E402

MESSAGE_TAGS = {_message_constants.ERROR: "danger"}

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
# One signed-in browser per account: a new login signs the previous one out (accounts/single_session.py).
# Set SINGLE_SESSION_PER_USER=False to allow any number of browsers again (no data change needed).
SINGLE_SESSION_PER_USER = env.bool("SINGLE_SESSION_PER_USER", default=True)
CSRF_COOKIE_HTTPONLY = True
CSRF_COOKIE_SAMESITE = "Lax"


# Password validation. MinimumLengthValidator's own default is 8 - raised
# to 10 given this app holds billing/PII data, not just chat transcripts.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
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
# Live Intelligence (chat/live_intelligence.py) - real headlines from public
# news feeds for the chat home page and one-click briefs. Set to False to turn
# the whole feature off (no outbound fetches; the home section says so).
LIVE_INTELLIGENCE_ENABLED = env.bool("LIVE_INTELLIGENCE_ENABLED", default=True)

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

# Server Media (SuperAdmin): storage-health thresholds for the media disk, in percent used.
# FAILED logins per IP per hour, across all usernames (credential stuffing; accounts/views.py). Only
# failures count, so many people signing in from one office address never reach it.
LOGIN_IP_FAILURE_LIMIT = env.int("LOGIN_IP_FAILURE_LIMIT", default=30)

MEDIA_DISK_WARN_PCT = env.int("MEDIA_DISK_WARN_PCT", default=80)
MEDIA_DISK_CRITICAL_PCT = env.int("MEDIA_DISK_CRITICAL_PCT", default=90)
# Server Media size filter: Small < MEDIUM_MIN <= Medium < LARGE_MIN <= Large, then the "large file"
# thresholds in MB (operational visibility only - nothing is ever deleted by size).
MEDIA_MEDIUM_MIN_BYTES = env.int("MEDIA_MEDIUM_MIN_BYTES", default=1024**2)
MEDIA_LARGE_MIN_BYTES = env.int("MEDIA_LARGE_MIN_BYTES", default=10 * 1024**2)
MEDIA_LARGE_THRESHOLDS_MB = env.list("MEDIA_LARGE_THRESHOLDS_MB", cast=int, default=[50, 100, 500])

# How much conversation a single request may send (chat/context_window.py). The provider metadata
# that would state each model's real window is not stored, so these are configured, deliberately
# conservative assumptions - never unlimited. Precedence: a MODEL_CONTEXT_TOKENS_BY_MODEL entry
# (model id substring -> tokens), then MODEL_CONTEXT_TOKENS (adapter type -> tokens), then the
# default. A plan's own max_context_tokens still applies on top (the smaller wins).
MODEL_CONTEXT_TOKENS_DEFAULT = env.int("MODEL_CONTEXT_TOKENS_DEFAULT", default=32000)
MODEL_CONTEXT_TOKENS = {
    "anthropic": 100000,
    "gemini": 100000,
    "openai_compatible": 32000,
    **env.json("MODEL_CONTEXT_TOKENS", default={}),
}
MODEL_CONTEXT_TOKENS_BY_MODEL = env.json("MODEL_CONTEXT_TOKENS_BY_MODEL", default={})

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
    # The two cookie flags default to FORCE_HTTPS (unchanged) but can be set on their own. A
    # deployment behind Cloudflare with FORCE_HTTPS=False (the origin only speaks plain HTTP) can
    # still mark the cookies Secure: that attribute is enforced by the visitor's browser on its
    # connection to Cloudflare, not by the origin, so it costs nothing and stops the session
    # cookie ever travelling over an http:// request.
    SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=FORCE_HTTPS)
    CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=FORCE_HTTPS)
    SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=31536000) if FORCE_HTTPS else 0
    SECURE_HSTS_INCLUDE_SUBDOMAINS = FORCE_HTTPS
    SECURE_HSTS_PRELOAD = FORCE_HTTPS
    # Nginx sits between Cloudflare and Gunicorn and sets this per deployment/nginx.conf.example.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


# HTTPS behind Cloudflare (accounts/middleware.py::CloudflareHttpsMiddleware). Both default OFF; the production
# docker-compose.yml turns them on. HSTS starts short (1 day) because a browser remembers it for that long:
# raise CLOUDFLARE_HSTS_SECONDS (e.g. 31536000) only after the redirect has run cleanly for a while.
ENFORCE_HTTPS_VIA_CLOUDFLARE = env.bool("ENFORCE_HTTPS_VIA_CLOUDFLARE", default=False)
CLOUDFLARE_HSTS_SECONDS = env.int("CLOUDFLARE_HSTS_SECONDS", default=0)
# Cloudflare's published proxy ranges (https://www.cloudflare.com/ips-v4 and /ips-v6, fetched 2026-09-21). Used by
# accounts/rate_limit.py::client_ip to decide whether a CF-Connecting-IP header can be believed. Override with a
# comma-separated list if Cloudflare changes them.
CLOUDFLARE_IP_RANGES = env.list(
    "CLOUDFLARE_IP_RANGES",
    default=[
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    ],
)


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
        # request_id/task_id come from accounts.middleware.RequestIDLogFilter
        # (below) - "-" on either when there's no request/task in scope
        # (e.g. a management command), so the format string never breaks.
        "verbose": {
            "format": "{asctime} {levelname} req={request_id} task={task_id} {name}: {message}",
            "style": "{",
        },
    },
    "filters": {
        "request_id": {"()": "accounts.middleware.RequestIDLogFilter"},
        # An expected 503 from /healthz/ is logged as a WARNING, not an ERROR -
        # see governance/error_alerts.py::HealthProbeDowngradeFilter.
        "health_probe": {"()": "governance.error_alerts.HealthProbeDowngradeFilter"},
        # Masks credential-shaped text (URLs with ?key=, Bearer tokens, sk-... keys)
        # in every message and traceback written to the console/file - see config/redaction.py.
        "redact_secrets": {"()": "config.redaction.SecretRedactionFilter"},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
            "filters": ["request_id", "redact_secrets"],
        },
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "app.log",
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "formatter": "verbose",
            "filters": ["request_id", "redact_secrets"],
        },
        # governance/error_alerts.py::AsyncAdminEmailHandler - same job as
        # Django's built-in AdminEmailHandler (email settings.ADMINS on
        # every unhandled 500, using Django's own SafeExceptionReporterFilter
        # traceback formatting), except the actual send is dispatched through
        # a Celery task (notifications/tasks.py::send_admin_error_alert)
        # instead of blocking the request on synchronous SMTP. A no-op
        # whenever ADMINS is empty, so a fresh deploy that hasn't set it yet
        # just logs to console/file as before.
        "mail_admins": {
            "level": "ERROR",
            "class": "governance.error_alerts.AsyncAdminEmailHandler",
        },
    },
    "root": {"handlers": ["console", "file"], "level": "INFO"},
    "loggers": {
        # Django's default logging config attaches its own synchronous
        # AdminEmailHandler to this logger, and "django.request" propagates
        # here - so every unhandled 500 reached TWO email handlers (this
        # project's async one below plus Django's stock one, one of which
        # blocked the request on SMTP). No handlers here: records still reach
        # console/file via "root", and the single alert path is "mail_admins".
        "django": {"handlers": [], "level": "INFO", "propagate": True},
        # This is the exact logger Django's request-handling machinery
        # writes to on every unhandled exception during a view (see
        # django.core.handlers.exception.handle_uncaught_exception) -
        # propagate=True so it still reaches console/file via "root" too.
        "django.request": {
            "handlers": ["mail_admins"],
            "filters": ["health_probe"],
            "level": "ERROR",
            "propagate": True,
        },
    },
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
    # Without this, Django's SMTP backend has NO socket timeout at all - an
    # unresponsive mail server hangs the connection indefinitely. Now that
    # every real email send goes through a Celery task (see accounts/tasks.py,
    # notifications/tasks.py::send_notification_email) rather than blocking
    # an HTTP request directly, a hang here would instead tie up a Celery
    # worker slot indefinitely - still worth bounding.
    EMAIL_TIMEOUT = env.int("EMAIL_TIMEOUT", default=10)
# Base URL used to build absolute links inside emails sent from a
# background task (Celery), where there's no request to call
# request.build_absolute_uri() on - notifications/emailing.py's
# open-tracking pixel is the current use. Set to the real deployed domain
# in production; the localhost default only matters for local dev, where
# nothing external will ever fetch the pixel anyway.
SITE_URL = env("SITE_URL", default="http://localhost:8000")

if not EMAIL_HOST:
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

# Who gets notified when an unhandled exception reaches a real request (see
# governance/error_alerts.py, wired to the "django.request" logger in
# LOGGING above) - a comma-separated list of "Name:email" pairs, e.g.
# ADMINS="Ops:ops@example.com,Founder:founder@example.com".
# Empty by default: a fresh deploy that hasn't set this yet just skips
# alerting rather than emailing no one and erroring on the send.
ADMINS = [tuple(pair.split(":", 1)) for pair in env.list("ADMINS", default=[]) if ":" in pair]
# The From address on those alert emails; SMTP servers often reject a
# message whose From doesn't match an authenticated sender, so this
# defaults to whatever DEFAULT_FROM_EMAIL already resolved to above
# rather than Django's own "root@localhost" default.
SERVER_EMAIL = env("SERVER_EMAIL", default=DEFAULT_FROM_EMAIL)

# Error monitoring — only active once SENTRY_DSN is set in .env. Silent no-op otherwise.
SENTRY_DSN = env("SENTRY_DSN", default="")
if SENTRY_DSN and "test" not in sys.argv:
    import sentry_sdk
    from sentry_sdk.integrations.celery import CeleryIntegration
    from sentry_sdk.integrations.django import DjangoIntegration

    def _sentry_before_send(event, hint):
        # Belt-and-suspenders on top of send_default_pii=False and
        # include_local_variables=False below: never forward the raw
        # request body (chat message content lives there) even if a
        # future SDK/integration change starts attaching it by default.
        request = event.get("request")
        if request and "data" in request:
            del request["data"]
        # Exception text/breadcrumbs can carry a request URL; mask any credential in it.
        from config.redaction import scrub_event

        return scrub_event(event)

    sentry_sdk.init(
        dsn=SENTRY_DSN,
        # CeleryIntegration: production-readiness audit gap - "Celery task
        # failures" was explicitly asked for when Sentry was first wired up,
        # but only DjangoIntegration was actually added. Without this, a
        # task that exhausts its retries (autoretry_for=(Exception,), see
        # e.g. notifications/tasks.py) only ever logged to console/file -
        # Sentry never saw it.
        integrations=[DjangoIntegration(), CeleryIntegration()],
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
