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
    """A personal UsageLimit overrides the user's department-level one."""
    personal = UsageLimit.objects.filter(user=user).first()
    if personal:
        return personal
    if user.department:
        return UsageLimit.objects.filter(department=user.department).first()
    return None


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
        raise UploadRejected(
            f"File type '.{file_extension or '?'}' isn't allowed. Allowed types: {', '.join(sorted(allowed_extensions))}."
        )


def check_usage_limits(user, conversation):
    """Raise UsageLimitExceeded if sending another message would (or already
    does) violate the user's effective daily/monthly/session/budget caps."""
    limit = _effective_limit(user)
    if limit is None:
        return

    now = timezone.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    assistant_messages = Message.objects.filter(
        conversation__user=user, role=Message.Role.ASSISTANT,
    )

    if limit.daily_token_cap is not None:
        used = assistant_messages.filter(created_at__gte=today_start).aggregate(
            total=Sum(F("input_tokens") + F("output_tokens")),
        )["total"] or 0
        if used >= limit.daily_token_cap:
            raise UsageLimitExceeded("Daily token limit reached. Try again tomorrow.")

    if limit.monthly_token_cap is not None:
        used = assistant_messages.filter(created_at__gte=month_start).aggregate(
            total=Sum(F("input_tokens") + F("output_tokens")),
        )["total"] or 0
        if used >= limit.monthly_token_cap:
            raise UsageLimitExceeded("Monthly token limit reached.")

    if limit.budget_cap_currency is not None:
        spent = assistant_messages.filter(created_at__gte=month_start).aggregate(
            total=Sum("estimated_cost"),
        )["total"] or 0
        if spent >= limit.budget_cap_currency:
            raise UsageLimitExceeded("Monthly budget cap reached.")

    if limit.session_limit is not None:
        sent = conversation.messages.filter(role=Message.Role.USER).count()
        if sent >= limit.session_limit:
            raise UsageLimitExceeded("Message limit reached for this conversation. Start a new chat.")
