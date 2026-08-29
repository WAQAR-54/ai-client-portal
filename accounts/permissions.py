"""RBAC helpers for view-level role enforcement.

Roles are hierarchical: admin > manager > user. `role_required` and
`RoleRequiredMixin` grant access to the given role and any role above it
in the hierarchy, unless `exact=True` is passed.
"""

from functools import wraps

from django.contrib.auth.mixins import AccessMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import redirect

from accounts.models import User

ROLE_LEVEL = {
    User.Role.USER: 0,
    User.Role.MANAGER: 1,
    User.Role.ADMIN: 2,
}


def _has_role(user, role, exact=False):
    if not user.is_authenticated:
        return False
    if exact:
        return user.role == role
    return ROLE_LEVEL.get(user.role, -1) >= ROLE_LEVEL.get(role, 0)


def role_required(role, exact=False):
    """View decorator: require `role` or higher (unless exact=True)."""

    def decorator(view_func):
        @wraps(view_func)
        def wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect("accounts:login")
            if not _has_role(request.user, role, exact=exact):
                raise PermissionDenied("You do not have access to this page.")
            return view_func(request, *args, **kwargs)

        return wrapped

    return decorator


class RoleRequiredMixin(AccessMixin):
    """Class-based view mixin: require `required_role` or higher."""

    required_role = None
    exact_role = False

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if self.required_role and not _has_role(request.user, self.required_role, exact=self.exact_role):
            raise PermissionDenied("You do not have access to this page.")
        return super().dispatch(request, *args, **kwargs)


class ManagerRequiredMixin(RoleRequiredMixin):
    required_role = User.Role.MANAGER


class AdminRequiredMixin(RoleRequiredMixin):
    required_role = User.Role.ADMIN
