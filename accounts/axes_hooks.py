from django.conf import settings
from django.http import HttpResponse
from django.template.loader import render_to_string


def axes_lockout_response(request, credentials):
    """Rendered instead of the login view once an account hits
    AXES_FAILURE_LIMIT failed attempts within AXES_COOLOFF_TIME - a clear,
    on-brand message rather than django-axes' plain-text default, and
    distinct from "invalid credentials" so a locked-out user isn't left
    guessing whether they just mistyped their password again."""
    cooloff_minutes = int(settings.AXES_COOLOFF_TIME.total_seconds() // 60)
    html = render_to_string(
        "accounts/locked_out.html",
        {"cooloff_minutes": cooloff_minutes},
        request=request,
    )
    return HttpResponse(html, status=429)
