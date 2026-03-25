"""
Order lifecycle V2 + can_transition / apply_transition tests.

Run: python manage.py test tea_supply.tests.test_order_status_flow_v2
"""

from django.contrib.auth.models import User
from django.test import TestCase

from tea_supply.models import Order, UserRole
from tea_supply.order_status_flow import apply_transition, can_transition


def _user(username: str, role: str) -> User:
    u = User.objects.create_user(username=username, password="pw-v2-flow")
    UserRole.objects.update_or_create(user=u, defaults={"role": role})
    return u


class OrderStatusFlowV2Tests(TestCase):
    def test_warehouse_cannot_paid_to_shipping(self):
        wh = _user("wh_flow", UserRole.Role.WAREHOUSE)
        o = Order.objects.create(name="t", status=Order.OrderStatus.PAID)
        ok, msg = can_transition(wh, o, Order.OrderStatus.SHIPPING)
        self.assertFalse(ok)

    def test_driver_cannot_complete_other_driver_order(self):
        da = _user("da_flow", UserRole.Role.DRIVER)
        db = _user("db_flow", UserRole.Role.DRIVER)
        o = Order.objects.create(
            name="t",
            status=Order.OrderStatus.SHIPPING,
            assigned_driver=db,
        )
        ok, msg = can_transition(da, o, Order.OrderStatus.COMPLETED)
        self.assertFalse(ok)

    def test_manager_cannot_skip_pending_to_shipping(self):
        mg = _user("mg_flow", UserRole.Role.MANAGER)
        o = Order.objects.create(name="t", status=Order.OrderStatus.PENDING)
        ok, msg = can_transition(mg, o, Order.OrderStatus.SHIPPING)
        self.assertFalse(ok)

    def test_customer_cannot_transition(self):
        cu = _user("cu_flow", UserRole.Role.CUSTOMER)
        o = Order.objects.create(name="t", status=Order.OrderStatus.PENDING)
        ok, msg = can_transition(cu, o, Order.OrderStatus.CONFIRMED)
        self.assertFalse(ok)

    def test_manager_paid_to_picking_forbidden(self):
        """Warehouse-only edge: paid → picking."""
        mg = _user("mg2_flow", UserRole.Role.MANAGER)
        o = Order.objects.create(name="t", status=Order.OrderStatus.PAID)
        ok, msg = can_transition(mg, o, Order.OrderStatus.PICKING)
        self.assertFalse(ok)

    def test_warehouse_paid_to_picking_ok(self):
        wh = _user("wh2_flow", UserRole.Role.WAREHOUSE)
        o = Order.objects.create(name="t", status=Order.OrderStatus.PAID)
        ok, _msg = can_transition(wh, o, Order.OrderStatus.PICKING)
        self.assertTrue(ok)

    def test_apply_transition_manager_confirmed_to_paid(self):
        mg = _user("mg3_flow", UserRole.Role.MANAGER)
        o = Order.objects.create(name="t", status=Order.OrderStatus.CONFIRMED)
        ok, _msg = apply_transition(mg, o.pk, Order.OrderStatus.PAID)
        self.assertTrue(ok)
        o.refresh_from_db()
        self.assertEqual(o.status, Order.OrderStatus.PAID)
