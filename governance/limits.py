from django.conf import settings
from django.db.models import F, Sum
from django.utils import timezone

from chat.models import Message
from governance.models import UsageLimit


class UsageLimitExceeded(Exception):
    pass


class UploadRejected(Exception):
    pass


def _effective_limit(user):
    """Precedence: personal UsageLimit > department UsageLimit > the user's
    Plan (see governance/plans.py) > no limit at all. Personal/department
    overrides existed before Plans did and still win outright when set —
    Plans are the new baseline underneath them, not a replacement."""
    personal = UsageLimit.objects.filter(user=user).first()
    if personal:
        return personal
    if user.department:
        department_limit = UsageLimit.objects.filter(department=user.department).first()
        if department_limit:
            return department_limit
    from governance.plans import plan_limit_fallback

    return plan_limit_fallback(user)


def validate_upload(user, uploaded_file):
    """Raise UploadRejected if `uploaded_file` violates this user's
    effective size/extension limits (personal, then department, then the
    system-wide default from settings)."""
    limit = _effective_limit(user)

    max_mb = (limit.max_upload_size_mb if limit else None) or settings.DEFAULT_MAX_UPLOAD_SIZE_MB
    max_bytes = max_mb * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise UploadRejected(f"File is too large. Max size is {max_mb}MB.")

    allowed_raw = (limit.allowed_file_extensions if limit else "") or settings.DEFAULT_ALLOWED_FILE_EXTENSIONS
    allowed_extensions = {ext.strip().lower().lstrip(".") for ext in allowed_raw.split(",") if ext.strip()}
    file_extension = uploaded_file.name.rsplit(".", 1)[-1].lower() if "." in uploaded_file.name else ""
    if file_extension not in allowed_extensions:
        allowed_list = ", ".join(sorted(allowed_extensions))
        raise UploadRejected(f"File type '.{file_extension or '?'}' isn't allowed. Allowed types: {allowed_list}.")


def check_usage_limits(user, conversation):
    """Raise UsageLimitExceeded if sending another message would (or already
    does) violate the user's effective daily/monthly/session/budget caps,
    or if their Plan has expired past its grace window."""
    from governance.plans import get_plan_status

    plan_state = get_plan_status(user)["state"]
    if plan_state == "expired":
        raise UsageLimitExceeded("Your trial has ended — contact your administrator.")
    if plan_state == "grace":
        raise UsageLimitExceeded(
            "Your trial has ended. You're in a short grace period with read-only access — "
            "contact your administrator to continue chatting."
        )

    limit = _effective_limit(user)
    if limit is None:
        return

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    assistant_messages = Message.objects.filter(
        conversation__user=user,
        role=Message.Role.ASSISTANT,
    )

    if limit.daily_token_cap is not None:
        used = (
            assistant_messages.filter(created_at__gte=today_start).aggregate(
                total=Sum(F("input_tokens") + F("output_tokens")),
            )["total"]
            or 0
        )
        if used >= limit.daily_token_cap:
            raise UsageLimitExceeded("Daily token limit reached. Try again tomorrow.")

    if limit.monthly_token_cap is not None:
        used = (
            assistant_messages.filter(created_at__gte=month_start).aggregate(
                total=Sum(F("input_tokens") + F("output_tokens")),
            )["total"]
            or 0
        )
        if used >= limit.monthly_token_cap:
            raise UsageLimitExceeded("Monthly token limit reached.")

    if limit.budget_cap_currency is not None:
        spent = (
            assistant_messages.filter(created_at__gte=month_start).aggregate(
                total=Sum("estimated_cost"),
            )["total"]
            or 0
        )
        if spent >= limit.budget_cap_currency:
            raise UsageLimitExceeded("Monthly budget cap reached.")

    if limit.session_limit is not None:
        sent = conversation.messages.filter(role=Message.Role.USER).count()
        if sent >= limit.session_limit:
            raise UsageLimitExceeded("Message limit reached for this conversation. Start a new chat.")


def _metric(label, used, cap):
    used = used or 0
    pct = min(100, round((used / cap) * 100)) if cap else 0
    level = "danger" if pct >= 100 else "warn" if pct >= 80 else "ok"
    return {"label": label, "used": used, "cap": cap, "pct": pct, "level": level}


def get_usage_status(user, conversation=None):
    """Read-only snapshot of this user's own usage vs. their effective
    limit, for the user-facing usage widget. Does not enforce anything —
    see check_usage_limits() for the actual gate. Never touches other
    users' data."""
    limit = _effective_limit(user)
    if limit is None:
        return {"has_limits": False, "metrics": [], "warn": False}

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    assistant_messages = Message.objects.filter(conversation__user=user, role=Message.Role.ASSISTANT)
    metrics = []

    if limit.daily_token_cap is not None:
        used = assistant_messages.filter(created_at__gte=today_start).aggregate(
            total=Sum(F("input_tokens") + F("output_tokens")),
        )["total"]
        metrics.append(_metric("Tokens today", used, limit.daily_token_cap))

    if limit.monthly_token_cap is not None:
        used = assistant_messages.filter(created_at__gte=month_start).aggregate(
            total=Sum(F("input_tokens") + F("output_tokens")),
        )["total"]
        metrics.append(_metric("Tokens this month", used, limit.monthly_token_cap))

    if limit.budget_cap_currency is not None:
        spent = assistant_messages.filter(created_at__gte=month_start).aggregate(
            total=Sum("estimated_cost"),
        )["total"]
        metrics.append(_metric("Budget this month", spent, limit.budget_cap_currency))
        metrics[-1]["is_currency"] = True

    if limit.session_limit is not None and conversation is not None:
        sent = conversation.messages.filter(role=Message.Role.USER).count()
        metrics.append(_metric("Messages in this chat", sent, limit.session_limit))

    worst = max(metrics, key=lambda m: m["pct"]) if metrics else None
    return {
        "has_limits": bool(metrics),
        "metrics": metrics,
        "warn": any(m["level"] != "ok" for m in metrics),
        "overall_pct": worst["pct"] if worst else 0,
        "overall_level": worst["level"] if worst else "ok",
    }
