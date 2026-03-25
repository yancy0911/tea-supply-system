from django.conf import settings

from tea_supply.models import UserRole
from tea_supply.rbac import get_effective_role


def currency(request):
    return {
        "currency_symbol": getattr(settings, "CURRENCY_SYMBOL", "$"),
    }


def portal_rbac(request):
    """Template helpers for nav / role checks (V1 RBAC)."""
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "portal_role": None,
            "is_portal_owner": False,
            "is_portal_manager": False,
            "is_portal_warehouse": False,
            "is_portal_driver": False,
            "is_portal_customer": False,
        }
    role = get_effective_role(user)
    return {
        "portal_role": role,
        "is_portal_owner": role == UserRole.Role.OWNER,
        "is_portal_manager": role == UserRole.Role.MANAGER,
        "is_portal_warehouse": role == UserRole.Role.WAREHOUSE,
        "is_portal_driver": role == UserRole.Role.DRIVER,
        "is_portal_customer": role == UserRole.Role.CUSTOMER,
    }

