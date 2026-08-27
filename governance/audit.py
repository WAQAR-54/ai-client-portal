from governance.models import AuditLog


def log_action(actor, action_type, target, old_value="", new_value=""):
    AuditLog.objects.create(
        actor=actor,
        action_type=action_type,
        target_type=target.__class__.__name__,
        target_id=str(target.pk),
        old_value=str(old_value),
        new_value=str(new_value),
    )
