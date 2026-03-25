"""
Role-based access control (V1).

Single source of truth: tea_supply.models.UserRole.role
Roles: owner, manager, warehouse, driver, customer.
"""

from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from tea_supply.models import UserRole


def get_effective_role(user):
    """
    Return UserRole.Role value for this user.
    Superusers are treated as owner for staff portal / reports.
    Users without a UserRole row get one with role=customer (lazy create).
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return UserRole.Role.OWNER
    rp = getattr(user, "role_profile", None)
    if rp is None:
        rp, _ = UserRole.objects.get_or_create(
            user=user, defaults={"role": UserRole.Role.CUSTOMER}
        )
    return rp.role


def is_staff_portal_role(role):
    """Non-customer roles that may use internal tools (subject to per-view rules)."""
    if not role:
        return False
    return role in (
        UserRole.Role.OWNER,
        UserRole.Role.MANAGER,
        UserRole.Role.WAREHOUSE,
        UserRole.Role.DRIVER,
    )


def role_required(*allowed_roles):
    """
    Restrict view to given roles (use UserRole.Role constants).
    - Unauthenticated -> redirect to login
    - customer on staff-only page -> redirect to shop home
    - other forbidden -> 403
    """

    allowed = frozenset(allowed_roles)

    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path(), login_url="/login/")
            role = get_effective_role(request.user)
            if role not in allowed:
                if role == UserRole.Role.CUSTOMER:
                    return redirect("shop-home")
                return HttpResponseForbidden("You do not have access to this page.")
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def owner_required(view_func):
    """Only owner (superuser counts as owner via get_effective_role)."""

    return role_required(UserRole.Role.OWNER)(view_func)


def staff_not_customer(view_func):
    """
    Any internal role except customer (owner/manager/warehouse/driver).
    Prefer role_required with explicit roles for new code.
    """

    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path(), login_url="/login/")
        role = get_effective_role(request.user)
        if not is_staff_portal_role(role):
            if role == UserRole.Role.CUSTOMER:
                return redirect("shop-home")
            return HttpResponseForbidden("You do not have access to this page.")
        return view_func(request, *args, **kwargs)

    return _wrapped
