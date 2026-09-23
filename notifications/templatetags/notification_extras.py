from django import template

from notifications.notify import notification_icon_kind

register = template.Library()


@register.filter
def wants_email(preference, notification_type):
    return preference.wants_email(notification_type)


@register.filter
def notif_icon(notification_type):
    """'security' | 'billing' | 'ai_system' | 'maintenance' | 'generic' - see
    notifications/notify.py::notification_icon_kind. The template picks the matching inline SVG."""
    return notification_icon_kind(notification_type)
