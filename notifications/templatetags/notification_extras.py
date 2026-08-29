from django import template

register = template.Library()


@register.filter
def wants_email(preference, notification_type):
    return preference.wants_email(notification_type)
