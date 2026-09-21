"""Maintenance Mode: the rules, in one place.

States (governance.models.MaintenanceWindow): OFF (no open row), SCHEDULED, ACTIVE, COMPLETED, CANCELLED.

    enable_now()  -> ACTIVE          schedule()  -> SCHEDULED
    SCHEDULED -> ACTIVE     when its start time arrives      SCHEDULED -> CANCELLED   cancel()
    ACTIVE    -> COMPLETED  end(), or when its end time arrives

Time is the source of truth, not a background job: `current_state()` (what the request middleware asks on every
request) applies any transition that is due, so a window starts and ends on time even if the Celery beat process is
down. The beat task `governance.tasks.advance_maintenance` only makes sure the emails and the audit rows appear on time
when nobody is visiting. Everything reuses what the app already has: log_action() for the audit trail, notify() (and so
the global email shell) for the emails, settings.TIME_ZONE for every displayed time.
"""

import logging
import re
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone, translation
from django.utils.translation import gettext as _

from governance.audit import log_action
from governance.models import MaintenanceWindow

logger = logging.getLogger(__name__)

# The most a single window may run: a typo in the end time must not lock everyone out for weeks.
MAX_WINDOW = timedelta(days=7)
REASON_MAX = 200
MESSAGE_MAX = 1000
STATE_KEY = "maintenance:state"
STATE_TTL = 30  # seconds: only a safety net; every change invalidates the key immediately

Status = MaintenanceWindow.Status
Kind = MaintenanceWindow.Kind
NOTICE_KINDS = ("scheduled", "started", "completed", "cancelled")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class MaintenanceError(ValueError):
    """A rule was broken (bad times, a conflicting window, ...). The text is safe to show the SuperAdmin."""


# ---------------------------------------------------------------- input


def clean_text(value, limit, multiline=False):
    """Plain text only: control characters removed, length capped. Nothing here is HTML - the templates escape it on
    output (autoescape, never |safe), so a message can never carry markup or script."""
    value = _CONTROL.sub("", str(value or "").replace("\r\n", "\n").replace("\r", "\n"))
    if not multiline:
        value = " ".join(value.split())
    else:
        value = "\n".join(line.rstrip() for line in value.split("\n")).strip()
    return value[:limit]


def portal_timezone():
    return ZoneInfo(settings.TIME_ZONE)


def parse_local(value):
    """'2026-09-22T14:30' (a datetime-local input) read in the portal's configured time zone -> aware datetime."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MaintenanceError(_("That date and time is not valid.")) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=portal_timezone())
    return parsed.astimezone(dt_timezone.utc)


def _validate_times(start, end, now):
    if start is not None and start <= now:
        raise MaintenanceError(_("The start time must be in the future."))
    if end is not None:
        if end <= (start or now):
            raise MaintenanceError(_("The end time must be after the start time."))
        if end - (start or now) > MAX_WINDOW:
            raise MaintenanceError(_("A maintenance window can last at most 7 days."))


# ---------------------------------------------------------------- state


def open_window():
    """The one SCHEDULED or ACTIVE window, or None (OFF)."""
    return MaintenanceWindow.objects.filter(open_slot=True).first()


def invalidate():
    try:
        cache.delete(STATE_KEY)
    except Exception:  # noqa: BLE001 - a cache outage must not break a state change
        logger.warning("Maintenance state cache could not be cleared", exc_info=True)


def _touch():
    invalidate()
    transaction.on_commit(invalidate)


def _stamp(value):
    return value.timestamp() if value else None


def _snapshot(window):
    return {
        "id": window.pk,
        "reason": window.reason,
        "message": window.message,
        "started": _stamp(window.actual_start),
        "end": _stamp(window.scheduled_end),
    }


def _load_state(now):
    advance(now)
    window = open_window()
    active = window if window and window.status == Status.ACTIVE else None
    if active:
        next_change = _stamp(active.scheduled_end)
    elif window:
        next_change = _stamp(window.scheduled_start)
    else:
        next_change = None
    return {"active": _snapshot(active) if active else None, "next_change": next_change}


def current_state(now=None):
    """A snapshot dict of the ACTIVE window, or None when the site is open. Cached; a transition due by the clock is
    applied first, so this is right to the second. Never raises: on any failure the site stays open (a maintenance check
    that errors must not lock everyone out)."""
    now = now or timezone.now()
    try:
        state = cache.get(STATE_KEY)
    except Exception:  # noqa: BLE001
        state = None
    try:
        if state is None or (state["next_change"] is not None and now.timestamp() >= state["next_change"]):
            state = _load_state(now)
            try:
                cache.set(STATE_KEY, state, STATE_TTL)
            except Exception:  # noqa: BLE001
                pass
        return state["active"]
    except Exception:  # noqa: BLE001
        logger.exception("Maintenance state could not be read; leaving the site open")
        return None


# ---------------------------------------------------------------- transitions


def _announce(window_id, kind):
    """Queue the emails for one transition (once: the task claims the notice atomically)."""
    from governance.tasks import send_maintenance_notice

    def queue():
        try:
            send_maintenance_notice.delay(window_id, kind)
        except Exception:  # noqa: BLE001 - a broker outage must not undo the state change
            logger.warning("Maintenance %s notice for window %s could not be queued", kind, window_id, exc_info=True)

    transaction.on_commit(queue)


def _start(window, now, actor=None):
    window.status = Status.ACTIVE
    window.actual_start = now
    window.save(update_fields=["status", "actual_start"])
    log_action(
        actor,
        "maintenance.enabled",
        window,
        old_value=Status.SCHEDULED if window.kind == Kind.SCHEDULED else "off",
        new_value=f"active: {window.reason}" + ("" if actor else " (automatic, at the scheduled start)"),
    )
    _announce(window.pk, "started")


def _close(window, status, now, actor=None, action="maintenance.completed", note=""):
    old = window.status
    window.status = status
    window.open_slot = None
    window.closed_by = actor
    window.closed_at = now
    fields = ["status", "open_slot", "closed_by", "closed_at"]
    if status == Status.COMPLETED:
        window.actual_end = now
        fields.append("actual_end")
    window.save(update_fields=fields)
    log_action(actor, action, window, old_value=old, new_value=f"{status}: {window.reason}{note}")
    _announce(window.pk, "completed" if status == Status.COMPLETED else "cancelled")


def _is_due(window, now):
    if window.status == Status.SCHEDULED:
        return bool(window.scheduled_start and window.scheduled_start <= now)
    return bool(window.status == Status.ACTIVE and window.scheduled_end and window.scheduled_end <= now)


def advance(now=None):
    """Apply every transition the clock has made due (start of a scheduled window, end of an active one)."""
    now = now or timezone.now()
    if not any(_is_due(window, now) for window in MaintenanceWindow.objects.filter(open_slot=True)):
        return  # the common case is one cheap read: no transaction, no row lock
    with transaction.atomic():
        for window in MaintenanceWindow.objects.select_for_update().filter(open_slot=True):
            if window.status == Status.SCHEDULED and window.scheduled_start and window.scheduled_start <= now:
                if window.scheduled_end and window.scheduled_end <= now:
                    # The whole window went by unseen (the server was down): it never ran, so it is cancelled,
                    # not "completed".
                    _close(
                        window,
                        Status.CANCELLED,
                        now,
                        action="maintenance.cancelled",
                        note=" (missed: the window had already passed)",
                    )
                else:
                    _start(window, now)
                _touch()
            elif window.status == Status.ACTIVE and window.scheduled_end and window.scheduled_end <= now:
                _close(window, Status.COMPLETED, now, note=" (automatic, at the end time)")
                _touch()


def _create(actor, kind, status, reason, message, start, end, notify_users):
    reason = clean_text(reason, REASON_MAX)
    if not reason:
        raise MaintenanceError(_("Enter a reason for the maintenance."))
    message = clean_text(message, MESSAGE_MAX, multiline=True)
    now = timezone.now()
    advance(now)
    if open_window() is not None:
        raise MaintenanceError(
            _("A maintenance window is already scheduled or active. End or cancel it before creating another.")
        )
    try:
        with transaction.atomic():
            window = MaintenanceWindow.objects.create(
                kind=kind,
                status=status,
                reason=reason,
                message=message,
                scheduled_start=start,
                scheduled_end=end,
                actual_start=now if status == Status.ACTIVE else None,
                created_by=actor,
                open_slot=True,
                notify_users=bool(notify_users),
            )
    except IntegrityError as exc:  # two SuperAdmins at the same moment: the unique open_slot decides
        raise MaintenanceError(
            _("A maintenance window is already scheduled or active. End or cancel it before creating another.")
        ) from exc
    _touch()
    return window


def enable_now(actor, reason, message="", end=None, notify_users=True):
    """Maintenance starts immediately. `end` (optional) is when it completes by itself."""
    _validate_times(None, end, timezone.now())
    window = _create(actor, Kind.IMMEDIATE, Status.ACTIVE, reason, message, None, end, notify_users)
    log_action(actor, "maintenance.enabled", window, old_value="off", new_value=f"active: {window.reason}")
    _announce(window.pk, "started")
    return window


def schedule(actor, reason, message, start, end, notify_users=True):
    """Maintenance starts by itself at `start` and completes by itself at `end` (both required)."""
    if start is None or end is None:
        raise MaintenanceError(_("A scheduled window needs both a start time and an end time."))
    _validate_times(start, end, timezone.now())
    window = _create(actor, Kind.SCHEDULED, Status.SCHEDULED, reason, message, start, end, notify_users)
    log_action(actor, "maintenance.scheduled", window, old_value="off", new_value=f"scheduled: {window.reason}")
    _announce(window.pk, "scheduled")
    return window


def _lock(window_id, expected):
    window = MaintenanceWindow.objects.select_for_update().filter(pk=window_id).first()
    if window is None or window.status != expected:
        raise MaintenanceError(
            _("That maintenance window is no longer %(state)s.") % {"state": str(Status(expected).label).lower()}
        )
    return window


def cancel(actor, window_id):
    """Withdraw a SCHEDULED window before it starts."""
    with transaction.atomic():
        window = _lock(window_id, Status.SCHEDULED)
        _close(window, Status.CANCELLED, timezone.now(), actor, action="maintenance.cancelled")
        _touch()
    return window


def end(actor, window_id):
    """End an ACTIVE window now."""
    with transaction.atomic():
        window = _lock(window_id, Status.ACTIVE)
        _close(window, Status.COMPLETED, timezone.now(), actor, action="maintenance.ended", note=" (ended by hand)")
        _touch()
    return window


# ---------------------------------------------------------------- what the pages show


def _moment(stamp):
    return datetime.fromtimestamp(stamp, tz=dt_timezone.utc) if stamp else None


def page_context(state, preview=False):
    """The context of the maintenance page: the reason and message (plain text, escaped by the template), the times in
    the portal's time zone (shown with its name) and the epoch used by the optional countdown."""
    state = state or {}
    return {
        "maintenance": {
            "reason": state.get("reason", ""),
            "message": state.get("message", ""),
            "started": _moment(state.get("started")),
            "expected_end": _moment(state.get("end")),
            "end_epoch": int(state["end"]) if state.get("end") else "",
            "now_epoch": int(timezone.now().timestamp()),
            "timezone": settings.TIME_ZONE,
            "support_email": getattr(settings, "SUPPORT_EMAIL", ""),
        },
        "preview": preview,
    }


def sample_state(now=None):
    """What the SuperAdmin's preview shows (nothing is activated)."""
    now = now or timezone.now()
    return {
        "reason": "Database upgrade",
        "message": "We are upgrading our storage. Chats and files are safe and will be back shortly.",
        "started": (now - timedelta(minutes=10)).timestamp(),
        "end": (now + timedelta(minutes=50)).timestamp(),
    }


def history(limit=20):
    """Compact history for the SuperAdmin: no message body, no e-mail addresses (a name, else the part before the @)."""
    rows = []
    for window in MaintenanceWindow.objects.select_related("created_by", "closed_by")[:limit]:
        rows.append(
            {
                "window": window,
                "initiated_by": person(window.created_by),
                "closed_by": person(window.closed_by) if window.closed_by_id else "",
            }
        )
    return rows


def person(user):
    if user is None:
        return _("(removed account)")
    return user.get_full_name() or user.email.split("@")[0]


# ---------------------------------------------------------------- notices


def _local(value):
    return value.astimezone(portal_timezone()).strftime("%b %d, %Y %H:%M") if value else ""


def _notice_text(window, kind):
    start = _local(window.scheduled_start or window.actual_start)
    end = _local(window.scheduled_end)
    tz = settings.TIME_ZONE
    if kind == "scheduled":
        title = _("Scheduled maintenance on %(start)s") % {"start": f"{start} {tz}"}
        body = _("The service will be unavailable from %(start)s to %(end)s (%(tz)s).") % {
            "start": start,
            "end": end,
            "tz": tz,
        }
    elif kind == "started":
        title = _("Maintenance has started")
        body = _("The service is unavailable while we work.") + (
            " " + _("We expect to finish by %(end)s (%(tz)s).") % {"end": end, "tz": tz} if end else ""
        )
    elif kind == "completed":
        title = _("Maintenance is complete")
        body = _("The service is available again. Thank you for your patience.")
    else:
        title = _("Scheduled maintenance cancelled")
        body = _("The maintenance planned for %(start)s will not take place. No action is needed.") % {
            "start": f"{start} {tz}"
        }
    return (
        title,
        body,
        {"kind": kind, "reason": window.reason, "message": window.message, "start": start, "end": end, "timezone": tz},
    )


def deliver_notice(window_id, kind):
    """Send one kind of notice for one window to the active users - exactly once. The claim is a single UPDATE of the
    window's own `notified_<kind>_at` column (only where it is still NULL); whoever wins it sends, everyone else stops.
    """
    from accounts.models import User
    from notifications.models import NotificationType
    from notifications.notify import notify

    if kind not in NOTICE_KINDS:
        raise ValueError(kind)
    window = MaintenanceWindow.objects.filter(pk=window_id).first()
    if window is None or not window.notify_users:
        return 0
    if kind == "cancelled" and window.notified_scheduled_at is None:
        return 0  # nobody was told about it, so there is nothing to withdraw
    claimed = MaintenanceWindow.objects.filter(pk=window_id, **{f"notified_{kind}_at__isnull": True}).update(
        **{f"notified_{kind}_at": timezone.now()}
    )
    if not claimed:
        return 0
    window.refresh_from_db()
    sent = 0
    for user in User.objects.filter(is_active=True).exclude(email="").iterator():
        with translation.override(user.preferred_language):
            title, body, meta = _notice_text(window, kind)
        try:
            notify(user, NotificationType.MAINTENANCE, title, body, metadata=meta)
            sent += 1
        except Exception:  # noqa: BLE001 - one bad recipient must not stop the rest
            logger.warning(
                "Maintenance notice %s for window %s failed for user %s", kind, window_id, user.pk, exc_info=True
            )
    logger.info("Maintenance %s notice for window %s sent to %s user(s)", kind, window_id, sent)
    return sent
