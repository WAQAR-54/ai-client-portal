from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from notifications.models import EMAIL_TOGGLE_LABELS, Notification, NotificationPreference


def _bell_context(request):
    notifications = Notification.objects.filter(user=request.user)[:10]
    unread_count = Notification.objects.filter(user=request.user, is_read=False).count()
    return {"notifications": notifications, "unread_count": unread_count}


@login_required
@require_GET
def bell_dropdown(request):
    return render(request, "notifications/_bell_dropdown.html", _bell_context(request))


@login_required
@require_http_methods(["POST"])
def mark_read(request, notification_id):
    notification = get_object_or_404(Notification, id=notification_id, user=request.user)
    notification.is_read = True
    notification.save(update_fields=["is_read"])
    return render(request, "notifications/_bell_dropdown.html", _bell_context(request))


@login_required
@require_http_methods(["POST"])
def mark_all_read(request):
    Notification.objects.filter(user=request.user, is_read=False).update(is_read=True)
    return render(request, "notifications/_bell_dropdown.html", _bell_context(request))


@login_required
@require_http_methods(["POST"])
def update_preferences(request):
    preference, _ = NotificationPreference.objects.get_or_create(user=request.user)
    for key, _label in EMAIL_TOGGLE_LABELS:
        field = f"email_{key}"
        setattr(preference, field, request.POST.get(field) == "on")
    preference.save()
    messages.success(request, "Notification preferences updated.")
    return redirect("accounts:profile")
