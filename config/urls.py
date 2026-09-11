from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView


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

    return serve(request, path, document_root=settings.MEDIA_ROOT)


def serve_docs(request, path):
    # Same reasoning as serve_media above - read settings.BASE_DIR inside
    # the view rather than baking it into urlpatterns at import time.
    from django.views.static import serve

    return serve(request, path, document_root=settings.BASE_DIR / "docs")


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("chat/", include("chat.urls")),
    path("governance/", include("governance.urls")),
    path("providers/", include("providers.urls")),
    path("notifications/", include("notifications.urls")),
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
    path("", RedirectView.as_view(pattern_name="accounts:dashboard", permanent=False)),
]
