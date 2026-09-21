"""The data behind the role-based dashboards (SuperAdmin, Admin, Manager, User).

Nothing here is new data: every number is read from something the application already keeps (users, conversations,
messages, projects, invoices, notifications, the audit log, System status, the usage limits). Rules that keep it honest:

* A number is only shown when it can be measured. Where it cannot, the tile says "Unavailable" - never a made-up zero.
* A trend ("up 14 %") is only shown when the comparison is meaningful (same hours of yesterday, a real baseline).
* One underlying problem becomes ONE "Needs attention" item (items carry a key and a key is only added once), and a
  notification that an attention item already covers is not listed a second time.
* Scope is decided by the same helpers the governance and billing screens use (`_scope_users`, `_scope_audit_logs`,
  `scoped_invoices`), so a dashboard can never show more than the page it links to. Links are only conveniences: the
  page behind each link still authorizes on its own.
* No private content: no conversation titles or messages of other people, no project names of other people, no audit
  old/new values, no keys or secrets. Team and department views show counts and names of people, never their work.
* Cheap: aggregates and bounded lists (5-8 rows), one query for several counts where they share a filter, no
  filesystem scan, no provider call, no Docker. System status is the one SuperAdmin probe the page already ran.
"""

from datetime import timedelta

from django.db.models import Count, F, Max, OuterRef, Q, Subquery, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _lazy

from accounts.models import Team, User
from chat.models import Conversation, Message, Project
from chat.utils import age_label
from governance.features import user_has_feature

# Trends are shown only against a baseline at least this big (10 % of 3 requests is noise, not information).
TREND_MIN_BASELINE = 10
ACTIVE_DAYS = 30  # a project or conversation counts as "active" if it was touched within this many days
RECENT_DAYS = 7

LEVEL_ORDER = {"danger": 0, "warn": 1, "info": 2}


# ---------------------------------------------------------------- small shared pieces


def display_name(user):
    """The first name if the profile has one, else the part of the e-mail before the @."""
    first = (user.first_name or "").strip()
    return first or user.email.split("@")[0].replace(".", " ").title()


def greeting(user):
    hour = timezone.localtime().hour
    part = _("morning") if hour < 12 else _("afternoon") if hour < 18 else _("evening")
    return {"part": part, "name": display_name(user)}


def _today_start():
    return timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)


def _month_start():
    return timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def tile(key, label, value, sub="", tone="muted", url=""):
    """One overview tile. value None = the number could not be measured: shown as "Unavailable"."""
    available = value is not None
    return {
        "key": key,
        "label": label,
        "value": value if available else _("Unavailable"),
        "available": available,
        "sub": sub,
        "tone": tone if available else "muted",
        "url": url,
    }


class Attention:
    """The "Needs attention" list. add() ignores a key it already holds, so the same problem reached from two data
    sources still appears once."""

    def __init__(self):
        self.items = []
        self._keys = set()

    def add(self, key, level, area, text, url, action):
        if key in self._keys:
            return
        self._keys.add(key)
        self.items.append({"key": key, "level": level, "area": area, "text": text, "url": url, "action": action})

    def sorted(self):
        return sorted(self.items, key=lambda item: LEVEL_ORDER[item["level"]])


def pluralize(count, one, many):
    return one if count == 1 else many


# ---------------------------------------------------------------- notifications


def _priority(notification_type):
    from notifications.models import NotificationType as T

    if notification_type in ACTION_TYPES():
        return "action"
    if notification_type in (T.USAGE_WARNING, T.TRIAL_EXPIRING, T.MAINTENANCE):
        return "warn"
    return "info"


_PRIORITY_ORDER = {"action": 0, "warn": 1, "info": 2}


def ACTION_TYPES():  # noqa: N802 - a constant that needs the notifications app loaded first
    from notifications.models import NotificationType as T

    return (T.INVOICE_PAYMENT_SUBMITTED, T.REFUND_REQUESTED, T.TRIAL_EXPIRED)


def notification_summary(user, exclude_types=(), limit=4):
    """Unread notifications, action-required first. `exclude_types` are kept in the count but not listed (an attention
    item already covers them). None when the role has notifications switched off."""
    if not user_has_feature(user, "notifications"):
        return None
    from notifications.models import Notification
    from notifications.notify import notification_action_url

    unread = Notification.objects.filter(user=user, is_read=False)
    total = unread.count()
    latest = list(unread.order_by("-created_at")[:25])
    action_required = unread.filter(notification_type__in=ACTION_TYPES()).count()
    listed = [n for n in latest if n.notification_type not in exclude_types]
    listed.sort(key=lambda n: (_PRIORITY_ORDER[_priority(n.notification_type)], -n.created_at.timestamp()))
    return {
        "unread": total,
        "action_required": action_required,
        "items": [
            {
                "title": n.title,
                "priority": _priority(n.notification_type),
                "url": notification_action_url(n) or reverse("notifications:list"),
                "age": age_label(n.created_at),
            }
            for n in listed[:limit]
        ],
        "url": reverse("notifications:list"),
    }


# ---------------------------------------------------------------- the user's own work (every role has some)


def chat_quick_actions(user):
    """The chat shortcuts this user can really use. A "post" action is the same form the chat home's quick-start cards
    submit (chat:create_conversation), so the dashboard adds no new workflow."""
    from governance.plans import has_feature

    new_conversation = reverse("chat:create_conversation")
    actions = [{"key": "new_conversation", "label": _("New conversation"), "url": new_conversation, "post": {}}]
    if user_has_feature(user, "projects"):
        actions.append(
            {"key": "new_project", "label": _("New project"), "url": reverse("chat:chat_home") + "#new-project"}
        )
    actions.append(
        {
            "key": "upload_document",
            "label": _("Upload document"),
            "url": new_conversation,
            "post": {"starter_text": _("Summarize this document and pull out the key points"), "start": "summarize"},
        }
    )
    if has_feature(user, "model_selection"):
        actions.append(
            {"key": "compare_models", "label": _("Compare models"), "url": new_conversation, "post": {"compare": "1"}}
        )
    return actions


def helpful_shortcuts(user):
    """Existing quick-start prompts that are not already a quick action (summarize lives in "Upload document")."""
    new_conversation = reverse("chat:create_conversation")
    return [
        {
            "key": "draft",
            "label": _("Draft a report"),
            "hint": _("Structured first pass, ready to edit"),
            "url": new_conversation,
            "post": {"starter_text": _("Draft a report on"), "start": "report"},
        },
        {
            "key": "code",
            "label": _("Review code"),
            "hint": _("Paste a snippet or a file"),
            "url": new_conversation,
            "post": {"starter_text": _("Review this code and flag issues:"), "start": "code"},
        },
    ]


def recent_conversations(user, limit=5):
    """This user's own newest conversations, bounded. Their own titles - nobody else's."""
    rows = Conversation.objects.filter(user=user).select_related("project").order_by("-updated_at")[:limit]
    return [
        {
            "id": c.id,
            "title": c.title,
            "project": c.project.name if c.project_id else "",
            "age": age_label(c.updated_at),
            "moment": c.updated_at,
            "url": reverse("chat:chat_conversation", kwargs={"conversation_id": c.id}),
        }
        for c in rows
    ]


def my_projects(user, limit=5):
    """The user's own projects, most recently worked-in first (bounded). Each links to its newest conversation."""
    if not user_has_feature(user, "projects"):
        return None
    alive = Q(conversations__is_deleted=False)
    newest = Conversation.objects.filter(project=OuterRef("pk")).order_by("-updated_at").values("id")[:1]
    rows = (
        Project.objects.filter(user=user)
        .annotate(
            last=Max("conversations__updated_at", filter=alive),
            count=Count("conversations", filter=alive),
            newest_id=Subquery(newest),
        )
        .order_by(F("last").desc(nulls_last=True), "name")[:limit]
    )
    return [
        {
            "name": p.name,
            "count": p.count,
            "age": age_label(p.last),
            "url": (
                reverse("chat:chat_conversation", kwargs={"conversation_id": p.newest_id})
                if p.newest_id
                else reverse("chat:chat_home")
            ),
        }
        for p in rows
    ]


# ---------------------------------------------------------------- USER


def user_dashboard(user):
    from governance.limits import get_usage_status

    conversations = recent_conversations(user, limit=5)
    continue_item = None
    if conversations:
        first = conversations[0]
        continue_item = {
            "title": first["title"],
            "subtitle": (_("Project: %(name)s") % {"name": first["project"]}) if first["project"] else "",
            "age": first["age"],
            "url": first["url"],
            "cta": _("Continue"),
        }
    usage = get_usage_status(user)
    return {
        "greeting": greeting(user),
        "continue_item": continue_item,
        "quick_actions": chat_quick_actions(user),
        "shortcuts": helpful_shortcuts(user),
        "projects": my_projects(user),
        "recent_conversations": conversations[1:] if continue_item else [],
        "has_conversations": bool(conversations),
        "notifications": notification_summary(user),
        "usage_warning": _usage_warning(usage),
    }


def _usage_warning(usage):
    """A clear heads-up when the user is close to or over a limit that already exists (no new billing logic)."""
    if not usage.get("has_limits") or not usage.get("warn"):
        return None
    level = usage.get("overall_level")
    return {
        "level": "danger" if level == "danger" else "warn",
        "text": (
            _("You've reached a usage limit.") if level == "danger" else _("You're approaching your usage limit.")
        ),
        "url": reverse("billing:my_plans"),
    }


# ---------------------------------------------------------------- shared aggregates


def ai_usage(assistant_messages, breakdown=False):
    """Requests (assistant replies) today / last 7 days / this month, and a trend against the SAME hours of yesterday.
    One aggregate query for the counts; the two provider/model breakdowns are two more, bounded to 30 days."""
    now = timezone.now()
    today, month = _today_start(), _month_start()
    week = now - timedelta(days=7)
    yesterday_start, yesterday_now = today - timedelta(days=1), now - timedelta(days=1)
    floor = min(month, week, yesterday_start)
    counts = assistant_messages.filter(created_at__gte=floor).aggregate(
        today=Count("id", filter=Q(created_at__gte=today)),
        week=Count("id", filter=Q(created_at__gte=week)),
        month=Count("id", filter=Q(created_at__gte=month)),
        baseline=Count("id", filter=Q(created_at__gte=yesterday_start, created_at__lt=yesterday_now)),
    )
    trend = None
    if counts["baseline"] >= TREND_MIN_BASELINE:
        pct = round((counts["today"] - counts["baseline"]) / counts["baseline"] * 100)
        trend = {"pct": abs(pct), "direction": "up" if pct > 0 else "down" if pct < 0 else "flat"}
    result = {
        "today": counts["today"],
        "week": counts["week"],
        "month": counts["month"],
        "trend": trend,
        "providers": [],
        "models": [],
    }
    if breakdown:
        since = now - timedelta(days=30)
        recent = assistant_messages.filter(created_at__gte=since, provider_model_used__isnull=False)
        result["providers"] = _bars(
            recent.values(label=F("provider_model_used__provider__name")).annotate(n=Count("id")).order_by("-n")[:5]
        )
        result["models"] = _bars(
            recent.values(label=F("provider_model_used__model_id")).annotate(n=Count("id")).order_by("-n")[:5]
        )
    return result


def _bars(rows):
    rows = list(rows)
    peak = max((r["n"] for r in rows), default=0) or 1
    return [{"label": r["label"], "value": r["n"], "pct": round(r["n"] / peak * 100)} for r in rows]


def project_counts(users):
    """Project totals for a set of people - counts only, never names (projects are personal)."""
    projects = Project.objects.filter(user__in=users)
    active_since = timezone.now() - timedelta(days=ACTIVE_DAYS)
    recent_since = timezone.now() - timedelta(days=RECENT_DAYS)
    touched = Conversation.objects.filter(user__in=users, project__isnull=False)
    return {
        "total": projects.count(),
        "active": touched.filter(updated_at__gte=active_since).values("project").distinct().count(),
        "recent": touched.filter(updated_at__gte=recent_since).values("project").distinct().count(),
    }


def _invoice_counts(invoices):
    from billing.models import Invoice

    today = timezone.localdate()
    return invoices.aggregate(
        unpaid=Count("id", filter=Q(status=Invoice.Status.UNPAID)),
        verification=Count("id", filter=Q(status=Invoice.Status.PENDING_VERIFICATION)),
        paid=Count("id", filter=Q(status=Invoice.Status.PAID)),
        overdue=Count("id", filter=Q(status=Invoice.Status.UNPAID, due_date__lt=today)),
    )


ACTIVITY_LABELS = {
    "user.create": "User created",
    "user.delete": "User deleted",
    "user.suspend": "User suspended",
    "user.activate": "User reactivated",
    "user.role_change": "Role changed",
    "user.plan_change": "Plan changed",
    "user.team_change": "Team changed",
    "user.department_change": "Department changed",
    "user.email_change": "Sign-in e-mail changed",
    "user.password_reset": "Password reset",
    "user.plan_cancelled": "Plan cancelled",
    "user.plan_cancellation_reversed": "Plan resumed",
    "upgrade_request.approve": "Plan request approved",
    "upgrade_request.dismiss": "Plan request dismissed",
    "team.add": "Team created",
    "team.delete": "Team deleted",
    "team.member_removed": "Team member removed",
    "provider.connect": "Provider connected",
    "provider.resync": "Provider synced",
    "provider.disconnect": "Provider disconnected",
    "provider.approve": "Provider approved",
    "provider.reject": "Provider rejected",
    "plan.create": "Plan created",
    "plan.update": "Plan updated",
    "model.enable": "Model enabled",
    "model.disable": "Model disabled",
    "providermodel.enable": "Model enabled",
    "providermodel.disable": "Model disabled",
    "limit.update": "Limit updated",
    "limit.delete": "Limit removed",
    "system_prompt.new_version": "System prompt updated",
    "security.mfa_required_toggle": "MFA requirement changed",
    "maintenance.scheduled": "Maintenance scheduled",
    "maintenance.enabled": "Maintenance started",
    "maintenance.ended": "Maintenance ended",
    "maintenance.completed": "Maintenance completed",
    "maintenance.cancelled": "Maintenance cancelled",
    "auth.lockout": "Account locked after failed sign-ins",
}
# Routine sign-in noise and other people's private housekeeping do not belong on a dashboard timeline.
ACTIVITY_HIDDEN = ("auth.login", "auth.session_superseded", "conversation.", "prompt_template.")


def _activity_label(action_type):
    label = ACTIVITY_LABELS.get(action_type)
    if label:
        return _(label)
    return action_type.replace(".", " ").replace("_", " ").capitalize()


def audit_activity(request, limit=8):
    """The newest audit entries the viewer is allowed to see (the audit log's own scoping): who did what, when. Never
    the old/new values (they can hold e-mail addresses or IPs)."""
    from governance.models import AuditLog
    from governance.views import _scope_audit_logs

    qs = AuditLog.objects.select_related("actor").order_by("-timestamp")
    for prefix in ACTIVITY_HIDDEN:
        qs = qs.exclude(action_type__startswith=prefix)
    rows = list(_scope_audit_logs(request, qs)[:limit])
    return [
        {
            "text": _activity_label(row.action_type),
            "who": display_name(row.actor) if row.actor_id else _("System"),
            "age": age_label(row.timestamp),
            "when": row.timestamp,
        }
        for row in rows
    ]


# Where "continue managing ..." leads, by the kind of thing the viewer last changed. SuperAdmin-only pages are marked so
# an Admin's history never produces a link to a page they cannot open.
_MANAGE_PAGES = (
    ("provider", _lazy("Provider configuration"), "providers:list", True),
    ("providermodel", _lazy("Provider configuration"), "providers:list", True),
    ("maintenance", _lazy("Maintenance"), "governance:maintenance", True),
    ("plan", _lazy("Plan management"), "governance:plan_manage", True),
    ("model", _lazy("Models"), "governance:models", True),
    ("user", _lazy("Users"), "governance:users", False),
    ("team", _lazy("Teams"), "governance:teams", False),
    ("upgrade_request", _lazy("Upgrade requests"), "governance:upgrade_requests", False),
    ("limit", _lazy("Limits"), "governance:limits", False),
)


def admin_continue(user):
    """Newest of: the viewer's own last conversation, or the last thing they changed in the admin area (from the audit
    log, mapped to the page that manages it). None when there is nothing real to continue."""
    from governance.models import AuditLog

    candidates = []
    convo = recent_conversations(user, limit=1)
    if convo:
        c = convo[0]
        candidates.append(
            {
                "moment": c["moment"],
                "title": c["title"],
                "subtitle": _("Conversation"),
                "age": c["age"],
                "url": c["url"],
                "cta": _("Continue"),
            }
        )
    # Signing in is also an audit entry; it is not something the viewer "was working on".
    last = AuditLog.objects.filter(actor=user).exclude(action_type__startswith="auth.").order_by("-timestamp").first()
    if last:
        prefix = last.action_type.split(".")[0]
        for key, label, url_name, superadmin_only in _MANAGE_PAGES:
            if prefix == key and (user.is_superadmin or not superadmin_only):
                candidates.append(
                    {
                        "moment": last.timestamp,
                        "title": label,
                        "subtitle": _("Continue managing"),
                        "age": age_label(last.timestamp),
                        "url": reverse(url_name),
                        "cta": _("Open"),
                    }
                )
                break
    return max(candidates, key=lambda c: c["moment"]) if candidates else None


# ---------------------------------------------------------------- SUPERADMIN


def _system_summary(system_status):
    """(tone, headline, problems) from the System status the page already computed - no second probe."""
    problems = 0
    if system_status["database"]["state"] != "healthy":
        problems += 1
    if system_status["redis"]["state"] == "unavailable":
        problems += 1
    jobs = system_status["jobs"]
    if jobs["failing"] or jobs["beat_stale"]:
        problems += 1
    if problems:
        return "danger", _("Needs attention"), problems
    return "ok", _("Healthy"), 0


def _attention_from_system(attention, system_status):
    status_url = reverse("governance:dashboard") + "#sys-status-title"
    if system_status["database"]["state"] != "healthy":
        attention.add("db", "danger", _("Database"), _("The database is not responding."), status_url, _("View status"))
    if system_status["redis"]["state"] == "unavailable":
        attention.add(
            "redis", "danger", _("Redis"), _("Redis is configured but not responding."), status_url, _("View status")
        )
    providers = system_status["providers"]
    for row in providers["rows"]:
        if row["state"] == "failed":
            attention.add(
                f"provider:{row['slug']}",
                "warn",
                _("Provider"),
                (
                    (_("%(name)s: %(reason)s") % {"name": row["name"], "reason": row["reason"]})
                    if row["reason"]
                    else _("%(name)s could not be synced.") % {"name": row["name"]}
                ),
                reverse("providers:list"),
                _("View provider"),
            )
    if providers["never_synced"]:
        attention.add(
            "provider:never",
            "warn",
            _("Provider"),
            _("%(n)s connected provider(s) never synced.") % {"n": providers["never_synced"]},
            reverse("providers:list"),
            _("View providers"),
        )
    jobs = system_status["jobs"]
    if jobs["beat_stale"]:
        attention.add(
            "beat",
            "danger",
            _("Background jobs"),
            _("The scheduler has stopped dispatching tasks."),
            status_url,
            _("View status"),
        )
    for row in jobs["rows"]:
        if row["health"] == "failing":
            backup = "backup" in row["task"].lower() or "backup" in row["name"].lower()
            attention.add(
                f"job:{row['task']}",
                "danger",
                _("Backup") if backup else _("Background jobs"),
                (_("%(name)s failed on its last run.") % {"name": row["name"]}),
                status_url,
                _("View status"),
            )
    for metric in system_status["server_health"]["metrics"]:
        if metric["key"] == "disk" and metric["state"] in ("warning", "critical"):
            attention.add(
                "disk",
                "danger" if metric["state"] == "critical" else "warn",
                _("Storage"),
                _("Disk is %(value)s full.") % {"value": metric["value"]},
                reverse("governance:media"),
                _("Open Media"),
            )
        elif metric["key"] in ("cpu", "memory") and metric["state"] == "critical":
            attention.add(
                metric["key"],
                "danger",
                _("Server"),
                _("%(label)s is critically high.") % {"label": metric["label"]},
                status_url,
                _("View status"),
            )


def _backup_configured():
    from accounts.management.commands.backup_database import is_configured

    return is_configured()


def superadmin_dashboard(request, system_status, org_usage, pending_upgrade_requests):
    from billing.models import RefundRequest
    from billing.views import scoped_invoices
    from governance import maintenance

    user = request.user
    now = timezone.now()
    week = now - timedelta(days=RECENT_DAYS)

    people = User.objects.aggregate(
        total=Count("id"),
        active=Count("id", filter=Q(is_active=True)),
        new=Count("id", filter=Q(date_joined__gte=week)),
    )
    conversations = Conversation.objects.aggregate(total=Count("id"), week=Count("id", filter=Q(created_at__gte=week)))
    projects = project_counts(User.objects.all())
    usage = ai_usage(Message.objects.filter(role=Message.Role.ASSISTANT), breakdown=True)
    invoices = _invoice_counts(scoped_invoices(user))
    refunds = RefundRequest.objects.filter(status=RefundRequest.Status.PENDING).count()
    window = maintenance.open_window()

    attention = Attention()
    _attention_from_system(attention, system_status)
    if not _backup_configured():
        attention.add(
            "backup",
            "warn",
            _("Backup"),
            _("Database backup is not configured."),
            reverse("governance:dashboard") + "#sys-status-title",
            _("View status"),
        )
    if invoices["verification"]:
        attention.add(
            "verification",
            "warn",
            _("Billing"),
            _("%(n)s payment %(word)s to verify.")
            % {"n": invoices["verification"], "word": pluralize(invoices["verification"], _("proof"), _("proofs"))},
            reverse("billing:invoices"),
            _("View invoices"),
        )
    if invoices["overdue"]:
        attention.add(
            "overdue",
            "warn",
            _("Billing"),
            _("%(n)s overdue %(word)s.")
            % {"n": invoices["overdue"], "word": pluralize(invoices["overdue"], _("invoice"), _("invoices"))},
            reverse("billing:invoices"),
            _("View invoices"),
        )
    if refunds:
        attention.add(
            "refunds",
            "warn",
            _("Billing"),
            _("%(n)s refund %(word)s waiting.")
            % {"n": refunds, "word": pluralize(refunds, _("request"), _("requests"))},
            reverse("billing:refund_requests"),
            _("Review"),
        )
    if pending_upgrade_requests:
        attention.add(
            "upgrades",
            "warn",
            _("Plans"),
            _("%(n)s plan %(word)s waiting.")
            % {"n": pending_upgrade_requests, "word": pluralize(pending_upgrade_requests, _("request"), _("requests"))},
            reverse("governance:upgrade_requests"),
            _("Review"),
        )
    if org_usage.get("has_data") and org_usage.get("over_80_count"):
        attention.add(
            "usage80",
            "info",
            _("Usage"),
            _("%(n)s user(s) are over 80 %% of their limit.") % {"n": org_usage["over_80_count"]},
            reverse("governance:limits"),
            _("View limits"),
        )
    if window is not None:
        if window.status == "active":
            attention.add(
                "maintenance",
                "warn",
                _("Maintenance"),
                _("Maintenance is active: everyone except SuperAdmins is locked out."),
                reverse("governance:maintenance"),
                _("Manage"),
            )
        else:
            attention.add(
                "maintenance",
                "info",
                _("Maintenance"),
                _("Maintenance is scheduled for %(when)s.")
                % {"when": timezone.localtime(window.scheduled_start).strftime("%b %d, %H:%M")},
                reverse("governance:maintenance"),
                _("Manage"),
            )
    items = attention.sorted()

    tone, headline, _problems = _system_summary(system_status)
    providers = system_status["providers"]
    disk = next((m for m in system_status["server_health"]["metrics"] if m["key"] == "disk"), None)
    provider_warnings = providers["attention"] + providers["never_synced"]
    if window is None:
        maintenance_tile = tile(
            "maintenance", _("Maintenance"), _("None"), _("Scheduled: none"), "ok", reverse("governance:maintenance")
        )
    elif window.status == "active":
        maintenance_tile = tile(
            "maintenance",
            _("Maintenance"),
            _("ACTIVE"),
            _("Site locked for non-admins"),
            "danger",
            reverse("governance:maintenance"),
        )
    else:
        maintenance_tile = tile(
            "maintenance",
            _("Maintenance"),
            _("Scheduled"),
            timezone.localtime(window.scheduled_start).strftime("%b %d, %H:%M"),
            "warn",
            reverse("governance:maintenance"),
        )
    trend = usage["trend"]
    overview = [
        tile("system", _("System"), headline, "", tone, reverse("governance:dashboard") + "#sys-status-title"),
        tile(
            "providers",
            _("AI providers"),
            _("%(n)s connected") % {"n": providers["total"]} if providers["total"] else _("None connected"),
            (
                _("%(n)s warning(s)") % {"n": provider_warnings}
                if provider_warnings
                else _("All synced") if providers["total"] else ""
            ),
            "warn" if provider_warnings else "ok" if providers["total"] else "muted",
            reverse("providers:list"),
        ),
        tile(
            "users",
            _("Users"),
            people["active"],
            _("+%(n)s this week") % {"n": people["new"]} if people["new"] else _("active"),
            "muted",
            reverse("governance:users"),
        ),
        tile(
            "requests",
            _("AI requests today"),
            usage["today"],
            (
                (
                    _("%(arrow)s %(pct)s %% vs same time yesterday")
                    % {
                        "arrow": "↑" if trend["direction"] == "up" else "↓" if trend["direction"] == "down" else "→",
                        "pct": trend["pct"],
                    }
                )
                if trend
                else _("%(n)s this week") % {"n": usage["week"]}
            ),
            "muted",
            reverse("governance:usage"),
        ),
        tile(
            "storage",
            _("Storage"),
            disk["value"] if disk and disk["available"] else None,
            "",
            {"healthy": "ok", "warning": "warn", "critical": "danger"}.get(disk["state"] if disk else "", "muted"),
            reverse("governance:media"),
        ),
        tile(
            "invoices",
            _("Invoices"),
            _("%(n)s pending") % {"n": invoices["unpaid"] + invoices["verification"]},
            (
                _("%(n)s to verify") % {"n": invoices["verification"]}
                if invoices["verification"]
                else _("%(n)s paid") % {"n": invoices["paid"]}
            ),
            "warn" if invoices["verification"] else "muted",
            reverse("billing:invoices"),
        ),
        maintenance_tile,
        tile(
            "alerts",
            _("Alerts"),
            len(items),
            _("Require attention") if items else _("All clear"),
            "warn" if items else "ok",
            "#needs-attention",
        ),
    ]
    return {
        "greeting": greeting(user),
        "overview": overview,
        "attention": items,
        "system_ok": not items,
        "business": {
            "users": people,
            "conversations": conversations,
            "projects": projects,
            "invoices": invoices,
        },
        "ai_usage": usage,
        "provider_errors": providers["attention"],
        "activity": audit_activity(request),
        "continue_item": admin_continue(user),
        "quick_actions": [
            {"key": "users", "label": _("Create user"), "url": reverse("governance:users")},
            {"key": "project", "label": _("Create project"), "url": reverse("chat:chat_home") + "#new-project"},
            {"key": "invoices", "label": _("View invoices"), "url": reverse("billing:invoices")},
            {"key": "providers", "label": _("Manage providers"), "url": reverse("providers:list")},
            {"key": "media", "label": _("Media"), "url": reverse("governance:media")},
            {
                "key": "status",
                "label": _("System status"),
                "url": reverse("governance:dashboard") + "#sys-status-title",
            },
            {"key": "maintenance", "label": _lazy("Maintenance"), "url": reverse("governance:maintenance")},
            {"key": "branding", "label": _("Branding"), "url": reverse("governance:brand_theme")},
            {"key": "audit", "label": _("Audit logs"), "url": reverse("governance:audit_logs")},
        ],
        "notifications": notification_summary(user, exclude_types=_covered_notification_types()),
        "usage_url": reverse("governance:usage"),
        "audit_url": reverse("governance:audit_logs"),
    }


def _covered_notification_types():
    from notifications.models import NotificationType as T

    return (T.INVOICE_PAYMENT_SUBMITTED, T.REFUND_REQUESTED)


# ---------------------------------------------------------------- ADMIN (department-scoped; an unscoped Admin sees all)


def admin_dashboard(request, org_usage, pending_upgrade_requests):
    from billing.views import _scoped_pending_refund_requests, scoped_invoices
    from governance.views import _is_scoped_admin, _scope_by_user_department, _scope_users

    user = request.user
    people_qs = _scope_users(request, User.objects.all())
    week = timezone.now() - timedelta(days=RECENT_DAYS)
    today = _today_start()

    people = people_qs.aggregate(total=Count("id"), active=Count("id", filter=Q(is_active=True)))
    active_week = (
        Message.objects.filter(role=Message.Role.USER, created_at__gte=week, conversation__user__in=people_qs)
        .values("conversation__user")
        .distinct()
        .count()
    )
    conversations_today = Conversation.objects.filter(user__in=people_qs, updated_at__gte=today).count()
    projects = project_counts(people_qs)
    usage = ai_usage(
        _scope_by_user_department(
            request, Message.objects.filter(role=Message.Role.ASSISTANT), "conversation__user__department_id"
        )
    )
    invoices = _invoice_counts(scoped_invoices(user))
    refunds = _scoped_pending_refund_requests(request).count()
    teams = (
        Team.objects.filter(department_id=user.department_id).count()
        if _is_scoped_admin(user)
        else Team.objects.count()
    )

    attention = Attention()
    if invoices["verification"]:
        attention.add(
            "verification",
            "warn",
            _("Billing"),
            _("%(n)s payment %(word)s to verify.")
            % {"n": invoices["verification"], "word": pluralize(invoices["verification"], _("proof"), _("proofs"))},
            reverse("billing:invoices"),
            _("Review"),
        )
    if invoices["overdue"]:
        attention.add(
            "overdue",
            "warn",
            _("Billing"),
            _("%(n)s overdue %(word)s.")
            % {"n": invoices["overdue"], "word": pluralize(invoices["overdue"], _("invoice"), _("invoices"))},
            reverse("billing:invoices"),
            _("View"),
        )
    if refunds:
        attention.add(
            "refunds",
            "warn",
            _("Billing"),
            _("%(n)s refund %(word)s waiting.")
            % {"n": refunds, "word": pluralize(refunds, _("request"), _("requests"))},
            reverse("billing:refund_requests"),
            _("Review"),
        )
    if pending_upgrade_requests and user_has_feature(user, "upgrade_requests"):
        attention.add(
            "upgrades",
            "warn",
            _("Plans"),
            _("%(n)s plan %(word)s waiting.")
            % {"n": pending_upgrade_requests, "word": pluralize(pending_upgrade_requests, _("request"), _("requests"))},
            reverse("governance:upgrade_requests"),
            _("Review"),
        )
    if org_usage.get("has_data") and org_usage.get("over_80_count") and user_has_feature(user, "limits"):
        attention.add(
            "usage80",
            "info",
            _("Usage"),
            _("%(n)s team member(s) are over 80 %% of their limit.") % {"n": org_usage["over_80_count"]},
            reverse("governance:limits"),
            _("Open"),
        )
    notes = notification_summary(user, exclude_types=_covered_notification_types())
    if notes and notes["action_required"]:
        attention.add(
            "notifications",
            "info",
            _("Notifications"),
            _("%(n)s notification(s) need action.") % {"n": notes["action_required"]},
            notes["url"],
            _("Open"),
        )

    overview = [
        tile(
            "users",
            _("Team members"),
            people["active"],
            _("%(n)s active this week") % {"n": active_week},
            "muted",
            reverse("governance:users"),
        ),
        tile(
            "projects",
            _("Active projects"),
            projects["active"],
            _("%(n)s updated this week") % {"n": projects["recent"]},
            "muted",
            reverse("chat:chat_home"),
        ),
        tile("conversations", _("Conversations today"), conversations_today, "", "muted", ""),
        tile(
            "approvals",
            _("Pending approvals"),
            invoices["verification"]
            + refunds
            + (pending_upgrade_requests if user_has_feature(user, "upgrade_requests") else 0),
            "",
            "warn" if (invoices["verification"] or refunds or pending_upgrade_requests) else "ok",
            "#needs-attention",
        ),
        tile(
            "invoices",
            _("Pending invoices"),
            invoices["unpaid"],
            _("%(n)s overdue") % {"n": invoices["overdue"]} if invoices["overdue"] else "",
            "warn" if invoices["overdue"] else "muted",
            reverse("billing:invoices"),
        ),
        tile(
            "notifications",
            _("Unread notifications"),
            notes["unread"] if notes else None,
            "",
            "muted",
            notes["url"] if notes else "",
        ),
    ]
    activity = audit_activity(request) if user_has_feature(user, "audit_logs") else []
    actions = [
        {"key": "project", "label": _("New project"), "url": reverse("chat:chat_home") + "#new-project"},
        {"key": "conversation", "label": _("New conversation"), "url": reverse("chat:create_conversation"), "post": {}},
        {"key": "invite", "label": _("Invite team member"), "url": reverse("governance:users")},
        {"key": "projects", "label": _("View projects"), "url": reverse("chat:chat_home")},
        {"key": "notifications", "label": _("Notifications"), "url": reverse("notifications:list")},
        {"key": "billing", "label": _("Billing"), "url": reverse("billing:invoices")},
    ]
    if not user_has_feature(user, "projects"):
        actions = [a for a in actions if a["key"] not in ("project", "projects")]
    if not user_has_feature(user, "notifications"):
        actions = [a for a in actions if a["key"] != "notifications"]
    return {
        "greeting": greeting(user),
        "overview": overview,
        "attention": attention.sorted(),
        "team": {"members": people["total"], "active_week": active_week, "teams": teams},
        "projects": projects,
        "ai_usage": usage,
        "activity": activity,
        "continue_item": admin_continue(user),
        "quick_actions": actions,
        "notifications": notes,
        "department": user.department.name if user.department_id else "",
        "usage_url": reverse("governance:usage") if user_has_feature(user, "usage_cost") else "",
        "audit_url": reverse("governance:audit_logs") if user_has_feature(user, "audit_logs") else "",
    }


# ---------------------------------------------------------------- MANAGER (their own team)


def manager_dashboard(request, team, members):
    """`members` is the team's User queryset. Counts and people's names only: no titles, messages or project names."""
    user = request.user
    today = _today_start()
    week = timezone.now() - timedelta(days=RECENT_DAYS)

    member_ids = members.values("id")
    active_week = (
        Message.objects.filter(role=Message.Role.USER, created_at__gte=week, conversation__user__in=member_ids)
        .values("conversation__user")
        .distinct()
        .count()
    )
    conversations_today = Conversation.objects.filter(user__in=member_ids, updated_at__gte=today).count()
    projects = project_counts(members)
    usage = ai_usage(Message.objects.filter(role=Message.Role.ASSISTANT, conversation__user__in=member_ids))
    over_80 = members_over_limit(members)
    notes = notification_summary(user)

    attention = Attention()
    if over_80:
        attention.add(
            "usage80",
            "warn",
            _("Usage"),
            _("%(n)s team member(s) are over 80 %% of their limit.") % {"n": over_80},
            "#team-activity",
            _("Review"),
        )
    if notes and notes["action_required"]:
        attention.add(
            "notifications",
            "warn",
            _("Notifications"),
            _("%(n)s notification(s) need action.") % {"n": notes["action_required"]},
            notes["url"],
            _("Open"),
        )
    if team is None:
        attention.add(
            "noteam",
            "info",
            _("Team"),
            _("You haven't been assigned a team yet - ask your Admin to assign you one."),
            reverse("chat:chat_home"),
            _("Open chat"),
        )
    elif not members.exists():
        attention.add(
            "nomembers",
            "info",
            _("Team"),
            _("Your team has no members yet - ask your Admin to add some."),
            "#team-activity",
            _("Open"),
        )

    member_count = members.count()
    overview = [
        tile(
            "members",
            _("Team members"),
            member_count,
            _("%(n)s active this week") % {"n": active_week},
            "muted",
            "#team-activity",
        ),
        tile(
            "projects",
            _("Active projects"),
            projects["active"],
            _("%(n)s updated this week") % {"n": projects["recent"]},
            "muted",
            reverse("chat:chat_home"),
        ),
        tile("conversations", _("Conversations today"), conversations_today, "", "muted", ""),
        tile(
            "notifications",
            _("Unread notifications"),
            notes["unread"] if notes else None,
            "",
            "muted",
            notes["url"] if notes else "",
        ),
    ]
    actions = [
        {"key": "conversation", "label": _("New conversation"), "url": reverse("chat:create_conversation"), "post": {}},
        {"key": "project", "label": _("New project"), "url": reverse("chat:chat_home") + "#new-project"},
        {"key": "projects", "label": _("View projects"), "url": reverse("chat:chat_home")},
        {"key": "team", "label": _("My team"), "url": "#team-activity"},
        {"key": "notifications", "label": _("Notifications"), "url": reverse("notifications:list")},
        {"key": "billing", "label": _("My billing"), "url": reverse("billing:my_invoices")},
    ]
    if not user_has_feature(user, "projects"):
        actions = [a for a in actions if a["key"] not in ("project", "projects")]
    if not user_has_feature(user, "notifications"):
        actions = [a for a in actions if a["key"] != "notifications"]

    convo = recent_conversations(user, limit=1)
    return {
        "greeting": greeting(user),
        "overview": overview,
        "attention": attention.sorted(),
        "projects": projects,
        "ai_usage": usage,
        "activity": team_activity(members),
        "continue_item": (
            {
                "title": convo[0]["title"],
                "subtitle": _("Conversation"),
                "age": convo[0]["age"],
                "url": convo[0]["url"],
                "cta": _("Continue"),
            }
            if convo
            else None
        ),
        "quick_actions": actions,
        "notifications": notes,
    }


def members_over_limit(members, pct=80):
    """How many of `members` have used at least `pct` % of their monthly token cap. Same precedence as the limits
    themselves (a personal limit, else the department's, else the plan's) but BATCHED: a fixed handful of queries
    for the whole team instead of several per person, so a big team costs the same as a small one."""
    from governance.models import UsageLimit
    from governance.plans import _PlanLimitFallback, get_plan_status

    people = list(members.select_related("plan_assignment__plan"))
    if not people:
        return 0
    personal = {row.user_id: row for row in UsageLimit.objects.filter(user__in=[u.pk for u in people])}
    department_ids = {u.department_id for u in people if u.department_id}
    by_department = {row.department_id: row for row in UsageLimit.objects.filter(department_id__in=department_ids)}
    used = dict(
        Message.objects.filter(
            role=Message.Role.ASSISTANT, created_at__gte=_month_start(), conversation__user__in=[u.pk for u in people]
        )
        .values("conversation__user")
        .annotate(total=Sum(F("input_tokens") + F("output_tokens")))
        .values_list("conversation__user", "total")
    )
    over = 0
    for person in people:
        limit = personal.get(person.pk) or by_department.get(person.department_id)
        if limit is None:
            plan = get_plan_status(person, assignment=getattr(person, "plan_assignment", None))["plan"]
            limit = _PlanLimitFallback(plan) if plan else None
        cap = limit.monthly_token_cap if limit else None
        if cap and (used.get(person.pk) or 0) / cap * 100 >= pct:
            over += 1
    return over


def team_activity(members, limit=8):
    """Who on the team started a conversation or a project, and when. Names of people and the kind of event only:
    a member's conversation titles and project names are theirs alone."""
    member_ids = members.values("id")
    convos = Conversation.objects.filter(user__in=member_ids).select_related("user").order_by("-created_at")[:limit]
    projects = Project.objects.filter(user__in=member_ids).select_related("user").order_by("-created_at")[:limit]
    events = [
        {"who": display_name(c.user), "text": _("started a conversation"), "when": c.created_at} for c in convos
    ] + [{"who": display_name(p.user), "text": _("created a project"), "when": p.created_at} for p in projects]
    events.sort(key=lambda e: e["when"], reverse=True)
    for event in events:
        event["age"] = age_label(event["when"])
    return events[:limit]
