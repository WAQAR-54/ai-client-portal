from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_http_methods

from governance.features import require_feature
from notifications.models import EMAIL_TOGGLE_LABELS, EmailLog, Notification, NotificationPreference
from notifications.notify import notification_action_url

# A real, minimal 1x1 transparent GIF - not a redirect to a static file,
# so this endpoint works standalone with no other dependency, and every
# request (opened or not) gets the exact same response either way; only
# whether EmailLog.opened_at got set differs.
_TRACKING_PIXEL_GIF = bytes.fromhex(
    "47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002020144003b"
)


@require_GET
def track_email_open(request, token):
    """Public (no login - an email client fetching this has no session)
    open-tracking pixel, embedded by notifications/emailing.py in every
    HTML email sent. Sets EmailLog.opened_at the first time it's fetched;
    a later fetch (re-opening the same email) leaves the original
    timestamp alone. An unknown/expired token still gets the pixel back -
    never a 404, since that would be a visibly broken image in the
    recipient's email client for no benefit to us."""
    EmailLog.objects.filter(tracking_token=token, opened_at__isnull=True).update(opened_at=timezone.now())
    return HttpResponse(_TRACKING_PIXEL_GIF, content_type="image/gif")


def _with_action_urls(notifications):
    """Attaches .action_url to each row (see notifications.notify.
    notification_action_url) - a plain attribute set here rather than a
    model property, since it needs reverse() and stays a cheap no-DB-hit
    computation either way."""
    notifications = list(notifications)
    for n in notifications:
        n.action_url = notification_action_url(n)
    return notifications


def _bell_context(request):
    notifications = _with_action_urls(Notification.objects.filter(user=request.user)[:10])
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    return {"notifications": notifications, "unread_count": unread_count}


@login_required
@require_feature("notifications")
@require_GET
def bell_dropdown(request):
    return render(request, "notifications/_bell_dropdown.html", _bell_context(request))


@login_required
@require_feature("notifications")
@require_GET
def notification_list(request):
    """Full history - the bell dropdown only ever shows the 10 most
    recent (see _bell_context), so anyone with more than that piling up
    had no way to ever see or act on the rest of their unread backlog
    except "Mark all read"."""
    paginator = Paginator(Notification.objects.filter(user=request.user), 25)
    page = paginator.get_page(request.GET.get("page"))
    page.object_list = _with_action_urls(page.object_list)
    return render(request, "notifications/list.html", {"page_obj": page})


@login_required
@require_feature("notifications")
@require_http_methods(["POST"])
def mark_read(request, notification_id):
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    if request.headers.get("HX-Request"):
        return render(request, "notifications/_bell_dropdown.html", _bell_context(request))
    return redirect(request.POST.get("next") or "notifications:list")


@login_required
@require_feature("notifications")
@require_http_methods(["POST"])
def mark_all_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    if request.headers.get("HX-Request"):
        return render(request, "notifications/_bell_dropdown.html", _bell_context(request))
    return redirect(request.POST.get("next") or "notifications:list")


@login_required
@require_feature("notifications")
@require_http_methods(["POST"])
def update_preferences(request):
    preference, _created = NotificationPreference.objects.get_or_create(user=request.user)
    for key, _label in EMAIL_TOGGLE_LABELS:
        field = f"email_{key}"
        setattr(preference, field, request.POST.get(field) == "on")
    preference.save()
    messages.success(request, _("Notification preferences updated."))
    return redirect("accounts:profile")
