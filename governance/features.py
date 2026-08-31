"""Role-wide feature visibility (see governance/models.py's
ADMIN_NAV_FEATURES / USER_CHAT_FEATURES / RoleFeatureToggle). A SuperAdmin
always has every feature - the whole point of the role is being unscoped -
so it never even queries the toggle table for that role.
"""

from functools import wraps

from django.core.exceptions import PermissionDenied

from accounts.models import User


def role_has_feature(role, feature_key):
    """True unless a SuperAdmin explicitly turned this feature off for this
    role. No row for (role, feature_key) means visible by default, so
    introducing a new feature (or this table itself) never silently hides
    something that used to work."""
    from governance.models import RoleFeatureToggle

    if role == User.Role.SUPERADMIN:
        return True
    toggle = RoleFeatureToggle.objects.filter(role=role, feature_key=feature_key).first()
    return toggle is None or toggle.is_enabled


def user_has_feature(user, feature_key):
    if not getattr(user, "is_authenticated", False):
        return False
    return role_has_feature(user.role, feature_key)


def require_feature(feature_key):
    """View decorator: 403 if the acting user's role has this feature
    switched off — the server-side half of the `has_feature` template
    filter, so a toggle is a real access-control decision, not just a
    hidden nav item. Stack under @role_required so the role check (and its
    redirect-to-login for anonymous users) still runs first."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not user_has_feature(request.user, feature_key):
                raise PermissionDenied("This feature isn't available for your role.")
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


class RequireFeatureMixin:
    """Class-based view equivalent of require_feature() above."""

    feature_key = None

    def dispatch(self, request, *args, **kwargs):
        if self.feature_key and not user_has_feature(request.user, self.feature_key):
            raise PermissionDenied("This feature isn't available for your role.")
        return super().dispatch(request, *args, **kwargs)
