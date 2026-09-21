"""SuperAdmin control page for Maintenance Mode (rules: governance/maintenance.py). Every view is SuperAdmin-only and
every change is a POST with a confirmation dialog on the page."""

from django.conf import settings
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from accounts.models import User
from accounts.permissions import role_required
from governance import maintenance


def _flag(request, name):
    return request.POST.get(name) == "1"


@role_required(User.Role.SUPERADMIN)
@require_GET
def maintenance_settings(request):
    maintenance.advance()  # a window whose time has come is applied before it is shown
    window = maintenance.open_window()
    status = window.status if window else "off"
    return render(
        request,
        "governance/maintenance.html",
        {
            "window": window,
            "status": status,
            "history": maintenance.history(),
            "timezone_name": settings.TIME_ZONE,
            "values": request.session.pop("maintenance_form", {}),
        },
    )


def _run(request, action, success):
    """Runs one maintenance action; a broken rule is shown as an error message, never a server error."""
    try:
        action()
    except maintenance.MaintenanceError as exc:
        messages.error(request, str(exc))
        # keep what was typed so the SuperAdmin does not have to enter it again
        request.session["maintenance_form"] = {
            key: request.POST.get(key, "") for key in ("reason", "message", "start", "end")
        }
    else:
        messages.success(request, success)
    return redirect("governance:maintenance")


@role_required(User.Role.SUPERADMIN)
@require_POST
def maintenance_enable_now(request):
    def action():
        maintenance.enable_now(
            request.user,
            request.POST.get("reason"),
            request.POST.get("message"),
            end=maintenance.parse_local(request.POST.get("end")),
            notify_users=_flag(request, "notify_users"),
        )

    return _run(request, action, _("Maintenance is now active. Only SuperAdmins can use the site."))


@role_required(User.Role.SUPERADMIN)
@require_POST
def maintenance_schedule(request):
    def action():
        maintenance.schedule(
            request.user,
            request.POST.get("reason"),
            request.POST.get("message"),
            maintenance.parse_local(request.POST.get("start")),
            maintenance.parse_local(request.POST.get("end")),
            notify_users=_flag(request, "notify_users"),
        )

    return _run(request, action, _("Maintenance is scheduled."))


@role_required(User.Role.SUPERADMIN)
@require_POST
def maintenance_cancel(request, window_id):
    return _run(
        request, lambda: maintenance.cancel(request.user, window_id), _("The scheduled maintenance was cancelled.")
    )


@role_required(User.Role.SUPERADMIN)
@require_POST
def maintenance_end(request, window_id):
    return _run(
        request, lambda: maintenance.end(request.user, window_id), _("Maintenance has ended. The site is open again.")
    )
