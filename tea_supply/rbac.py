"""
Role-based access control (V1 / V1.5 hardening).

Single source of truth: tea_supply.models.UserRole.role
Roles: owner, manager, warehouse, driver, customer.

All staff portal views must use decorators from this module — never rely on templates alone.

Response policy (V1.5):
- Not authenticated → redirect to /login/?next=...
- customer on a view that does not allow CUSTOMER → redirect to shop home (/)
- Any other disallowed role → 403 Forbidden
"""

from functools import wraps
from typing import Optional

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme

from tea_supply.models import UserRole


def get_effective_role(user):
    """
    Return UserRole.Role value for this user.
    Superusers are treated as owner for staff portal / reports.
    Staff (is_staff) without a UserRole row are treated as owner (no lazy create).
    Other users without a UserRole row get one with role=customer (lazy create).
    """
    if not user or not getattr(user, "is_authenticated", False):
        return None
    if getattr(user, "is_superuser", False):
        return UserRole.Role.OWNER
    rp = getattr(user, "role_profile", None)
    if rp is None:
        rp = UserRole.objects.filter(user=user).first()
    if rp is None:
        if getattr(user, "is_staff", False):
            return UserRole.Role.OWNER
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


def _safe_next_url(request, url: Optional[str]) -> Optional[str]:
    """Reject open redirects; allow same-site relative paths and safe absolute URLs."""
    s = (url or "").strip()
    if not s:
        return None
    if s.startswith("/") and not s.startswith("//"):
        return s
    if url_has_allowed_host_and_scheme(
        s,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return s
    return None


def _login_redirect_role(user) -> str:
    """
    Role used only for post-login redirect (does not lazy-create UserRole for staff).
    """
    if not user or not getattr(user, "is_authenticated", False):
        return UserRole.Role.CUSTOMER
    if getattr(user, "is_superuser", False):
        return UserRole.Role.OWNER
    rp = UserRole.objects.filter(user=user).first()
    if rp is not None:
        return rp.role
    if getattr(user, "is_staff", False):
        return UserRole.Role.OWNER
    return UserRole.Role.CUSTOMER


def _default_home_for_role(role: str) -> str:
    if role == UserRole.Role.OWNER:
        return reverse("boss-dashboard")
    if role == UserRole.Role.MANAGER:
        return reverse("orders-list")
    if role == UserRole.Role.WAREHOUSE:
        return reverse("inventory-list")
    if role == UserRole.Role.DRIVER:
        return reverse("driver-orders")
    return reverse("shop-home")


# Path prefixes (no trailing slash required on keys; matching is prefix-safe).
_NEXT_ALLOWED_PREFIXES = {
    UserRole.Role.OWNER: (
        "/dashboard",
        "/orders",
        "/reports",
        "/inventory",
        "/replenishment",
        "/customers",
        "/admin",
        "/order",
        "/profile",
        "/credit",
        "/",
    ),
    UserRole.Role.MANAGER: (
        "/dashboard",
        "/orders",
        "/reports",
        "/customers",
        "/order",
        "/profile",
        "/credit",
        "/",
    ),
    UserRole.Role.WAREHOUSE: ("/inventory", "/replenishment", "/order", "/profile"),
    UserRole.Role.DRIVER: ("/driver", "/profile"),
    UserRole.Role.CUSTOMER: (
        "/shop",
        "/my-orders",
        "/profile",
        "/credit",
        "/checkout",
    ),
}


def _path_allowed_for_role(path: str, role: str) -> bool:
    path = (path or "").strip()
    path = path.split("?")[0]
    if not path.startswith("/"):
        path = "/" + path if path else "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    prefixes = _NEXT_ALLOWED_PREFIXES.get(role, ())
    for prefix in prefixes:
        if prefix == "/":
            if path in ("", "/"):
                return True
            continue
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def get_post_login_redirect(request, user, next_url: Optional[str] = None) -> str:
    """
    After successful login: use ``next`` only if safe (same-site) and allowed for the
    user's role; otherwise the role default home.

    Staff/superuser with no UserRole row → default /dashboard/ (and owner-like ``next`` rules).
    Non-staff with no UserRole → /shop/ defaults (customer).
    """
    role = _login_redirect_role(user)
    default = _default_home_for_role(role)
    safe = _safe_next_url(request, next_url)
    if not safe:
        return default
    if _path_allowed_for_role(safe, role):
        return safe
    return default


def resolve_login_redirect_url(request, user, *, next_url: Optional[str] = None) -> str:
    """Backwards-compatible alias for :func:`get_post_login_redirect`."""
    return get_post_login_redirect(request, user, next_url)


def role_required(*allowed_roles):
    """
    Restrict view to given roles (use UserRole.Role constants).
    - Unauthenticated → redirect to login
    - customer when not allowed → redirect to shop home (name ``shop-home``)
    - wrong internal role → 403
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


def staff_required(view_func):
    """
    Authenticated user must be an internal staff role (owner / manager / warehouse / driver).
    customer → redirect to shop; missing/other → 403.

    Prefer :func:`role_required` with an explicit role list for new views.
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


# Backwards-compatible alias
staff_not_customer = staff_required
