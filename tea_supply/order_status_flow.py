"""
Order lifecycle (V2): single source for allowed status transitions by role.

All status changes must go through can_transition() + apply_transition() except
system payment callbacks (Stripe/bank) which use apply_payment_paid_system().
"""

from __future__ import annotations

from typing import Optional, Tuple

from django.db import transaction
from django.utils import timezone

from tea_supply.credit_debt import apply_credit_debt_if_needed, reverse_credit_debt_if_counted
from tea_supply.models import Order, UserRole, update_customer_level
from tea_supply.rbac import get_effective_role


def can_transition(user, order: Order, target_status: str) -> Tuple[bool, str]:
    """
    Return (allowed, error_message).
    Owner may move to any valid OrderStatus (including cancelled).
    """
    valid = {c[0] for c in Order.OrderStatus.choices}
    if target_status not in valid:
        return False, "Invalid target status."

    role = get_effective_role(user)
    cur = str(order.status)

    if target_status == cur:
        return True, ""

    if role == UserRole.Role.CUSTOMER:
        return False, "Customers cannot change order status."

    if role == UserRole.Role.OWNER:
        return True, ""

    if role == UserRole.Role.MANAGER:
        allowed_edges = {
            (Order.OrderStatus.PENDING, Order.OrderStatus.CONFIRMED),
            (Order.OrderStatus.CONFIRMED, Order.OrderStatus.PAID),
            (Order.OrderStatus.PAID, Order.OrderStatus.SHIPPING),
            (Order.OrderStatus.PICKING, Order.OrderStatus.SHIPPING),
        }
        if (cur, target_status) in allowed_edges:
            return True, ""
        return False, "This transition is not allowed for your role."

    if role == UserRole.Role.WAREHOUSE:
        if (cur, target_status) == (Order.OrderStatus.PAID, Order.OrderStatus.PICKING):
            return True, ""
        return False, "This transition is not allowed for your role."

    if role == UserRole.Role.DRIVER:
        if (cur, target_status) != (Order.OrderStatus.SHIPPING, Order.OrderStatus.COMPLETED):
            return False, "This transition is not allowed for your role."
        if order.assigned_driver_id != user.id:
            return False, "You are not the assigned driver for this order."
        return True, ""

    return False, "Forbidden."


@transaction.atomic
def apply_transition(user, order_id: int, target_status: str) -> Tuple[bool, str]:
    """
    Enforce can_transition, persist Order.status, payment_status/paid_at when entering PAID,
    and run credit side-effects (same semantics as legacy order_status_update).
    """
    order = Order.objects.select_for_update().get(pk=order_id)
    ok, msg = can_transition(user, order, target_status)
    if not ok:
        return False, msg

    old = str(order.status)

    order.status = target_status

    update_fields = ["status"]

    if target_status == Order.OrderStatus.PAID and old != Order.OrderStatus.PAID:
        if order.paid_at is None:
            order.paid_at = timezone.now()
            update_fields.append("paid_at")
        order.payment_status = Order.PaymentStatus.PAID
        update_fields.append("payment_status")

    order.save(update_fields=update_fields)

    # Credit debt side effects (mirror legacy views)
    if (
        target_status == Order.OrderStatus.CONFIRMED
        and old == Order.OrderStatus.PENDING
    ):
        apply_credit_debt_if_needed(order)
    if target_status == Order.OrderStatus.PAID and old == Order.OrderStatus.PENDING:
        apply_credit_debt_if_needed(order)

    if target_status == Order.OrderStatus.CANCELLED and old != Order.OrderStatus.CANCELLED:
        reverse_credit_debt_if_counted(order)

    if target_status == Order.OrderStatus.PAID and old != Order.OrderStatus.PAID:
        reverse_credit_debt_if_counted(order)

    order.refresh_from_db()
    if order.customer_id and target_status in (
        Order.OrderStatus.CONFIRMED,
        Order.OrderStatus.PAID,
    ):
        update_customer_level(order.customer)

    return True, ""


@transaction.atomic
def apply_payment_paid_system(
    order_id: int, *, payment_method: Optional[str] = None
) -> Tuple[bool, str]:
    """
    Called from payment webhooks (Stripe success, etc.) — no end-user role.
    Moves order to PAID lifecycle and marks payment received.
    """
    order = Order.objects.select_for_update().get(pk=order_id)
    old = str(order.status)
    order.status = Order.OrderStatus.PAID
    if order.paid_at is None:
        order.paid_at = timezone.now()
    order.payment_status = Order.PaymentStatus.PAID
    update_fields = ["status", "paid_at", "payment_status"]
    if payment_method is not None:
        order.payment_method = payment_method
        update_fields.append("payment_method")
    order.save(update_fields=update_fields)
    if old != Order.OrderStatus.PAID:
        reverse_credit_debt_if_counted(order)
    order.refresh_from_db()
    if order.customer_id:
        update_customer_level(order.customer)
    return True, ""
