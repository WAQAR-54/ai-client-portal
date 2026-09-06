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


def user_can_access_standalone_tool(user, feature_key):
    """Access rule shared by every standalone tool reached by direct link
    only (Code Playground, Domain Generator, ...) - never linked from the
    main sidebar nav. Just user_has_feature(), named separately so call
    sites read as "does this role have this standalone tool" rather than
    a generic feature check - SuperAdmin always has it (role_has_feature's
    own unconditional rule); Admin/Manager/User default to having it too,
    since these tools' seeding migrations only seed an explicit
    is_enabled=False row for "user"/"manager", not "admin" (see e.g.
    governance's 0019_admin_keeps_default_standalone_tool_access) - "no
    row" reads as visible, same default as every other feature. A
    SuperAdmin can still explicitly turn any of these off for any role,
    Admin included, from Feature Visibility - that's a real access
    decision (this function is what both the page and the admin
    Dashboard's link/stats consult), not just a hidden nav item."""
    return user_has_feature(user, feature_key)


def require_standalone_tool_access(feature_key):
    """Function-view decorator version of user_can_access_standalone_tool.
    Assumes @login_required already ran (stack this one under it) so
    request.user is a real authenticated User, never AnonymousUser (which
    has no .is_admin)."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not user_can_access_standalone_tool(request.user, feature_key):
                raise PermissionDenied("This tool isn't available for your role.")
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


class RequireStandaloneToolAccessMixin:
    """CBV equivalent of require_standalone_tool_access() - must come after
    LoginRequiredMixin in the base class list so its dispatch (the auth
    check/login redirect) runs first; only once request.user is a real
    authenticated User does this mixin's dispatch check the role."""

    feature_key = None

    def dispatch(self, request, *args, **kwargs):
        if not user_can_access_standalone_tool(request.user, self.feature_key):
            raise PermissionDenied("This tool isn't available for your role.")
        return super().dispatch(request, *args, **kwargs)
