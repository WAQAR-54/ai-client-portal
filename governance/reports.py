"""Pure aggregation helpers for the admin Reports section (Revenue / Usage /
Growth) - same split as governance/limits.py and governance/plans.py: no
View classes or URL-facing code here, only the query/aggregation logic that
governance/views.py's report Views and CSV export functions call into.

Every summary function takes `request` (for role-based department scoping
and ?date_from=/?date_to= GET params) and returns a plain dict merged
straight into the view's template context - the same shape is reused by
that report's CSV export so "the export matches what's on screen" is true
by construction, exactly like governance/views.py's
_filtered_assistant_messages is shared between UsageSummaryView and its
exports.

No new tracking model is introduced. Everything here is computed from data
that already exists (Invoice, Message, User) - notably, "how often did
someone hit a usage limit" is NOT reportable this way, since
governance.limits.UsageLimitExceeded is only ever raised live and never
persisted anywhere (confirmed: not written to AuditLog or any other table).
"""

from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, F, Sum
from django.db.models.functions import TruncDate, TruncMonth
from django.utils import timezone

from accounts.models import User


# Same rule, deliberately re-declared rather than imported from
# governance/views.py or shared across app boundaries - matches the
# existing precedent of billing/views.py's own separate copy of this exact
# check rather than a new cross-module dependency for three lines of logic.
def _is_scoped_admin(user):
    return user.role == User.Role.ADMIN


def _last_n_months(today, n):
    """Ascending list of `date`s, the 1st of each of the last `n` calendar
    months including the current one."""
    months = []
    year, month = today.year, today.month
    for _ in range(n):
        months.append(date(year, month, 1))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(months))


def _bar_series(labels, values):
    """(label, value, pct-of-peak) triples for the CSS bar-chart classes
    already in main.css (.bar-chart/.bar-col/.bar-fill, see
    templates/governance/usage.html) - same shape UsageSummaryView already
    builds for its own 7-day chart, reused here so every report's chart
    renders with the exact same markup/CSS, no new chart code needed."""
    peak = max(values) or 1
    pct = [round(v / peak * 100) for v in values]
    return list(zip(labels, values, pct))


def revenue_summary(request):
    """Invoice-based revenue rollup: totals (invoiced/collected/outstanding/
    overdue), breakdowns by status/plan/currency (and, SuperAdmin only,
    by department), and a 6-month trend of collected (paid) revenue by the
    month it was issued. Scoped exactly like billing.views._invoices_context:
    a scoped Admin sees only their own department's invoices (a
    department-less invoice is invisible to them, matching that existing
    convention), a SuperAdmin sees everything."""
    from billing.models import Invoice

    qs = Invoice.objects.all()
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)

    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    if date_from:
        qs = qs.filter(issue_date__gte=date_from)
    if date_to:
        qs = qs.filter(issue_date__lte=date_to)

    today = timezone.localdate()
    total_invoiced = qs.aggregate(total=Sum("total"))["total"] or Decimal("0")
    collected = qs.filter(status=Invoice.Status.PAID).aggregate(total=Sum("total"))["total"] or Decimal("0")
    outstanding_qs = qs.exclude(status=Invoice.Status.PAID)
    outstanding = outstanding_qs.aggregate(total=Sum("total"))["total"] or Decimal("0")
    overdue_qs = outstanding_qs.filter(due_date__lt=today)
    overdue_amount = overdue_qs.aggregate(total=Sum("total"))["total"] or Decimal("0")

    status_labels = dict(Invoice.Status.choices)
    by_status = list(qs.values("status").annotate(count=Count("id"), total=Sum("total")).order_by("status"))
    for row in by_status:
        row["label"] = status_labels.get(row["status"], row["status"])

    by_plan = [
        {"name": row["plan__name"], "count": row["count"], "total": row["total"] or Decimal("0")}
        for row in qs.values("plan__name").annotate(count=Count("id"), total=Sum("total")).order_by("-total")[:10]
    ]
    by_currency = [
        {"currency": row["currency"], "count": row["count"], "total": row["total"] or Decimal("0")}
        for row in qs.values("currency").annotate(count=Count("id"), total=Sum("total")).order_by("-total")
    ]

    by_department = None
    if not _is_scoped_admin(request.user):
        by_department = [
            {
                "name": row["department__name"] or "(no department)",
                "count": row["count"],
                "total": row["total"] or Decimal("0"),
            }
            for row in qs.values("department__name").annotate(count=Count("id"), total=Sum("total")).order_by("-total")
        ]

    # issue_date is a plain DateField (not a DateTimeField), so TruncMonth
    # is timezone-safe here - no local/UTC conversion ambiguity like
    # date_joined below in growth_summary().
    months = _last_n_months(today, 6)
    monthly_rows = (
        qs.filter(status=Invoice.Status.PAID)
        .annotate(month=TruncMonth("issue_date"))
        .values("month")
        .annotate(total=Sum("total"))
    )
    monthly_by_key = {row["month"]: float(row["total"] or 0) for row in monthly_rows}
    monthly_trend = [monthly_by_key.get(m, 0.0) for m in months]
    monthly_bars = _bar_series([m.strftime("%b %Y") for m in months], monthly_trend)

    return {
        "invoice_count": qs.count(),
        "total_invoiced": total_invoiced,
        "total_collected": collected,
        "total_outstanding": outstanding,
        "overdue_count": overdue_qs.count(),
        "overdue_amount": overdue_amount,
        "by_status": by_status,
        "by_plan": by_plan,
        "by_currency": by_currency,
        "by_department": by_department,
        "monthly_bars": monthly_bars,
        "has_monthly_data": any(monthly_trend),
        "date_from": date_from,
        "date_to": date_to,
    }


def revenue_report_rows(request):
    """The raw, scoped+filtered Invoice queryset behind revenue_summary()
    (same filters, unaggregated) - for the CSV export, so "export matches
    the report" holds without re-deriving the filter logic a second time."""
    from billing.models import Invoice

    qs = Invoice.objects.select_related("department", "plan", "recipient_user")
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)
    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    if date_from:
        qs = qs.filter(issue_date__gte=date_from)
    if date_to:
        qs = qs.filter(issue_date__lte=date_to)
    return qs.order_by("-issue_date", "-id")


def usage_rollup_summary(request):
    """Cost/token rollup grouped by Plan and (SuperAdmin only) by
    Department - distinct from UsageSummaryView's existing per-user table,
    which this deliberately doesn't duplicate. Scoped the same way as
    governance.views._scope_by_user_department. A 14-day daily-cost trend,
    matching the width of DashboardView's own chart rather than
    UsageSummaryView's shorter 7-day one."""
    from chat.models import Message

    qs = Message.objects.filter(role=Message.Role.ASSISTANT)
    if _is_scoped_admin(request.user):
        qs = qs.filter(conversation__user__department_id=request.user.department_id)

    date_from = request.GET.get("date_from", "").strip()
    date_to = request.GET.get("date_to", "").strip()
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)

    totals = qs.aggregate(
        requests=Count("id"), tokens=Sum(F("input_tokens") + F("output_tokens")), cost=Sum("estimated_cost")
    )

    by_plan = [
        {
            "name": row["conversation__user__plan_assignment__plan__name"] or "(no plan)",
            "requests": row["requests"],
            "tokens": row["tokens"] or 0,
            "cost": row["cost"] or Decimal("0"),
        }
        for row in qs.values("conversation__user__plan_assignment__plan__name")
        .annotate(requests=Count("id"), tokens=Sum(F("input_tokens") + F("output_tokens")), cost=Sum("estimated_cost"))
        .order_by("-cost")
    ]

    by_department = None
    if not _is_scoped_admin(request.user):
        by_department = [
            {
                "name": row["conversation__user__department__name"] or "(no department)",
                "requests": row["requests"],
                "tokens": row["tokens"] or 0,
                "cost": row["cost"] or Decimal("0"),
            }
            for row in qs.values("conversation__user__department__name")
            .annotate(
                requests=Count("id"), tokens=Sum(F("input_tokens") + F("output_tokens")), cost=Sum("estimated_cost")
            )
            .order_by("-cost")
        ]

    today = timezone.localdate()
    window_start = today - timedelta(days=13)
    daily_rows = (
        qs.filter(created_at__date__gte=window_start)
        .annotate(day=TruncDate("created_at"))
        .values("day")
        .annotate(cost=Sum("estimated_cost"))
    )
    cost_by_day = {row["day"]: float(row["cost"] or 0) for row in daily_rows}
    day_range = [window_start + timedelta(days=i) for i in range(14)]
    daily_cost = [cost_by_day.get(d, 0.0) for d in day_range]
    daily_bars = _bar_series([d.strftime("%d %b") for d in day_range], daily_cost)

    return {
        "total_requests": totals["requests"] or 0,
        "total_tokens": totals["tokens"] or 0,
        "total_cost": totals["cost"] or Decimal("0"),
        "by_plan": by_plan,
        "by_department": by_department,
        "daily_bars": daily_bars,
        "has_daily_data": any(daily_cost),
        "date_from": date_from,
        "date_to": date_to,
    }


def growth_summary(request):
    """Signup/headcount rollup: current totals (active/suspended, by role,
    and, SuperAdmin only, by department) plus a 6-month new-signups trend.
    Scoped like governance.views._scope_users. The monthly trend is bucketed
    in Python from date_joined rather than a TruncMonth() DB query, since
    date_joined is a DateTimeField and TruncMonth's UTC/local-timezone
    boundary would otherwise need its own careful handling to line up with
    the plain-date months list - simplest to just localize each timestamp
    explicitly instead (there is no realistic user-table size in this app
    where that costs anything)."""
    qs = User.objects.all()
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)

    total_users = qs.count()
    active_count = qs.filter(is_active=True).count()

    role_labels = dict(User.Role.choices)
    by_role = [
        {"role": row["role"], "label": role_labels.get(row["role"], row["role"]), "count": row["count"]}
        for row in qs.values("role").annotate(count=Count("id")).order_by("role")
    ]

    by_department = None
    if not _is_scoped_admin(request.user):
        by_department = [
            {"name": row["department__name"] or "(no department)", "count": row["count"]}
            for row in qs.values("department__name").annotate(count=Count("id")).order_by("-count")
        ]

    today = timezone.localdate()
    months = _last_n_months(today, 6)
    bucket_counts = Counter()
    for joined_at in qs.values_list("date_joined", flat=True):
        local_dt = timezone.localtime(joined_at) if timezone.is_aware(joined_at) else joined_at
        bucket_counts[date(local_dt.year, local_dt.month, 1)] += 1
    monthly_trend = [bucket_counts.get(m, 0) for m in months]
    monthly_bars = _bar_series([m.strftime("%b %Y") for m in months], monthly_trend)

    return {
        "total_users": total_users,
        "active_count": active_count,
        "suspended_count": total_users - active_count,
        "by_role": by_role,
        "by_department": by_department,
        "monthly_bars": monthly_bars,
        "has_monthly_data": any(monthly_trend),
    }


def growth_report_rows(request):
    """The raw, scoped User queryset behind growth_summary() - for the CSV
    export (a user list export doesn't exist anywhere else in the app; the
    Users admin page's table has no export button)."""
    qs = User.objects.select_related("department").order_by("-date_joined")
    if _is_scoped_admin(request.user):
        qs = qs.filter(department_id=request.user.department_id)
    return qs
