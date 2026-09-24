import posixpath
import re

from django.conf import settings
from django.contrib import admin
from django.http import Http404, JsonResponse
from django.urls import include, path, re_path
from django.views.generic import RedirectView

from accounts.views import public_home_view
from config.client_errors import client_error
from config.health import HEALTHY, NOT_CONFIGURED, check_database, check_redis

# Only branding (the logo and favicon, which the login page needs before anyone is signed in)
# is public. Everything else under MEDIA_ROOT is private and is served by an authenticated
# view: chat attachments by chat:download_attachment, payment proofs by billing:invoice_proof.
# Their paths are predictable (chat_attachments/user_<id>/<yyyy>/<mm>/<the user's filename>),
# and this route used to hand any of them to an anonymous visitor.
PUBLIC_MEDIA_PREFIXES = ("branding/",)


def serve_media(request, path):
    # A thin wrapper, not django.views.static.serve directly registered
    # with document_root=settings.MEDIA_ROOT in the urlpatterns dict below
    # - that dict literal would capture whatever settings.MEDIA_ROOT
    # happened to be at the moment this module is first imported, once,
    # for the life of the process. Harmless in production (the setting
    # never changes at runtime there), but every test that overrides
    # MEDIA_ROOT to a throwaway temp dir would still 404 against the
    # real one. Reading settings.MEDIA_ROOT inside the view instead reads
    # the current value on every request.
    from django.views.static import serve

    # Check the NORMALISED path: "branding/../chat_attachments/x" starts with "branding/" but
    # django.views.static.serve collapses the ".." and would happily serve the private file.
    clean = posixpath.normpath(path).lstrip("/")
    if clean.startswith("..") or not clean.startswith(PUBLIC_MEDIA_PREFIXES):
        raise Http404
    return serve(request, clean, document_root=settings.MEDIA_ROOT)


def healthz(request):
    """Deeper than "Gunicorn answered a request" (which is all
    deployment/healthcheck.py's own default target, "/", ever proved) -
    actually runs a query against the real database connection, since
    that's the dependency most likely to be up/down independently of the
    Django process itself (e.g. Postgres restarting, a connection-pool
    exhaustion). Deliberately not gated behind DEBUG/auth - both Docker's
    own HEALTHCHECK and .github/workflows/ci.yml's post-deploy check hit
    this anonymously, from inside/outside the container respectively.
    Cheap by design (SELECT 1, no cache/Celery/S3 round-trip) - this
    needs to answer fast and often, not be a full dependency audit."""
    if check_database()["state"] != HEALTHY:
        # Fixed string, never the exception: this endpoint is anonymous.
        return JsonResponse({"status": "error", "database": "unavailable"}, status=503)
    return JsonResponse({"status": "ok"})


def healthz_deep(request):
    """A slower, more thorough sibling of healthz() above - checks Redis
    too, not just the database. Deliberately a SEPARATE endpoint rather
    than added to healthz() itself: that one is polled constantly (Docker's
    own HEALTHCHECK, CI's post-deploy gate) and its own docstring is
    explicit that staying cheap is the point - Redis/Celery/storage were a
    deliberate exclusion, not an oversight. This one is for an occasional
    manual/monitoring check instead, not wired into anything that polls
    it often.

    Celery worker liveness is deliberately NOT checked here either - it
    already has its own, better mechanism: docker-compose.yml's `worker`
    service runs `celery inspect ping` as its own Docker healthcheck,
    which round-trips through Redis to prove the worker is actually
    consuming tasks (not just alive). Duplicating that through an HTTP
    endpoint on the WEB process would mean this view blocking on a
    Celery control-plane round-trip it doesn't own and can't bound
    reliably. File storage isn't checked either - there's no S3/cloud
    storage configured (see docs/BACKUP_RESTORE.md), only local disk
    that already has to be writable for the process to have started at
    all, so there's nothing distinct left to verify there."""
    database = check_database()["state"]
    redis_state = check_redis()["state"]
    checks = {
        "database": "ok" if database == HEALTHY else database,
        "redis": "ok" if redis_state == HEALTHY else redis_state,
    }
    healthy = database == HEALTHY and redis_state in (HEALTHY, NOT_CONFIGURED)
    return JsonResponse({"status": "ok" if healthy else "error", **checks}, status=200 if healthy else 503)


# Only the plain-language guides are meant to be public. docs/ also holds operational
# notes (SECRETS.md, PRODUCTION_ACCESS.md, LOCAL_ACCESS.md, BACKUP_RESTORE.md) that
# must never be served: the route used to serve the whole folder, and the only thing
# keeping those files off the internet in production was .dockerignore leaving docs/
# out of the image. An allowlist makes the route safe on any build.
_PUBLIC_DOCS = re.compile(r"^(?:guides/[A-Za-z0-9_-]+\.html|FEATURE_GUIDE\.html)$")


def serve_docs(request, path):
    # Same reasoning as serve_media above - read settings.BASE_DIR inside
    # the view rather than baking it into urlpatterns at import time.
    from django.views.static import serve

    if not _PUBLIC_DOCS.match(path):
        raise Http404
    return serve(request, path, document_root=settings.BASE_DIR / "docs")


urlpatterns = [
    path("healthz/", healthz),
    path("healthz/deep/", healthz_deep),
    path("client-errors/", client_error),
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("billing/", include("billing.urls")),
    path("legal/", include("legal.urls")),
    path("chat/", include("chat.urls")),
    path("governance/", include("governance.urls")),
    path("providers/", include("providers.urls")),
    path("notifications/", include("notifications.urls")),
    path("search/", include("search.urls")),
    path("playground/", include("playground.urls")),
    path("domains/", include("domaingen.urls")),
    # No S3/CDN is configured for user-uploaded media (SiteBranding's logo/
    # favicon, chat attachments) and neither the app nor deployment/
    # nginx.conf.example ever had a route for MEDIA_URL - uploads were
    # saving to disk correctly, but the URL to actually load them back
    # (e.g. the login page's <img src="/media/branding/...">, or a
    # favicon <link>, which browsers fetch with no auth header at all) had
    # nothing to answer it and 404'd. Deliberately NOT gated behind
    # settings.DEBUG - unlike Django's docs default advice for a
    # large-scale deployment, this is the only thing serving these files
    # in production right now, S3 or a dedicated Nginx location being the
    # eventual, more scalable alternative.
    re_path(r"^media/(?P<path>.*)$", serve_media),
    # The plain-language guides in docs/ (docs/guides/index.html, user.html,
    # manager.html, admin.html, superadmin.html, plus the older single-page
    # docs/FEATURE_GUIDE.html) only lived as files in the git repo - no live
    # URL to actually open or share one. Served publicly, deliberately not
    # behind login_required: someone deciding whether to use the app, or
    # sharing a link with a teammate who doesn't have an account yet,
    # should be able to read these without first logging in. The URL path
    # mirrors the real docs/ folder exactly (e.g. /docs/guides/user.html),
    # so the relative links between these files (index.html's
    # "../FEATURE_GUIDE.html", each guide's "user.html"/"manager.html" ...)
    # resolve identically whether opened live or straight off disk.
    # Convenience redirects so /docs/ and /docs/guides/ alone (no filename)
    # land on the actual hub page instead of 404ing - these must come
    # BEFORE the catch-all re_path below, since the URL resolver checks
    # patterns in order and the catch-all would otherwise match first
    # (with path="") and try to serve the docs/ directory itself.
    path("docs/", RedirectView.as_view(url="/docs/guides/index.html", permanent=False)),
    path("docs/guides/", RedirectView.as_view(url="/docs/guides/index.html", permanent=False)),
    re_path(r"^docs/(?P<path>.*)$", serve_docs),
    # The public marketing/home page for an anonymous visitor - previously this redirected
    # straight to accounts:dashboard, which (being @login_required) just bounced an anonymous
    # visitor on to the login page with no explanation of the product first. An authenticated
    # visitor is still sent to their dashboard - see public_home_view.
    path("", public_home_view, name="home"),
]
