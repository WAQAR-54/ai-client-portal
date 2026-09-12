import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

from accounts.models import User

logger = logging.getLogger(__name__)


@receiver(post_save, sender=User)
def assign_default_plan_on_creation(sender, instance, created, **kwargs):
    if not created:
        return
    from governance.plans import assign_default_plan_if_missing

    assign_default_plan_if_missing(instance)


@receiver(post_save, sender=User)
def generate_welcome_invoice_on_creation(sender, instance, created, **kwargs):
    """Every new account - self-signup or admin-created, department
    assigned or not - gets an invoice for whatever plan it starts on
    (always the seeded "Demo" plan by default, via the receiver above,
    which Django guarantees runs first since it's connected first for
    this same signal+sender). A separate receiver rather than folding
    into assign_default_plan_on_creation so a bug in one can never stop
    the other from running."""
    if not created:
        return
    from billing.invoicing import InvoiceGenerationError, generate_invoice_for_user

    try:
        generate_invoice_for_user(instance)
    except InvoiceGenerationError:
        # No RegionalPrice configured yet for this plan/region on a fresh
        # install - the welcome invoice simply doesn't exist until a
        # SuperAdmin prices it. Never blocks account creation.
        pass
    except Exception:
        logger.exception("Failed to generate welcome invoice for user %s", instance.pk)


class _UnresolvedLoginTarget:
    """Stand-in audit-log target for a lockout whose username didn't match
    a real account (e.g. a pure brute-force guess against a nonexistent
    email) - log_action() only needs .pk and a class name to describe it."""

    pk = None

    def __init__(self, username):
        self.username = username

    def __str__(self):
        return self.username or "(unknown)"


def _log_axes_lockout(sender, request, username, ip_address, **kwargs):
    """Every account lockout is written to the audit log so an admin can
    spot a real attack pattern (many lockouts, many accounts, one IP) via
    the existing Audit Logs screen's search/filter, rather than only ever
    seeing this in django-axes' own admin-only AccessAttempt table."""
    from governance.audit import log_action

    target = User.objects.filter(email=username).first() or _UnresolvedLoginTarget(username)
    log_action(
        actor=None,
        action_type="auth.lockout",
        target=target,
        new_value=f"ip={ip_address}",
    )


def connect_axes_signals():
    from axes.signals import user_locked_out

    user_locked_out.connect(_log_axes_lockout, dispatch_uid="accounts.log_axes_lockout")
