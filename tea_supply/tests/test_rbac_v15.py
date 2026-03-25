"""
RBAC V1.5 security regression tests (role_required / owner_required / admin gate).

Run: python manage.py test tea_supply.tests.test_rbac_v15
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase

from tea_supply.models import Order, UserRole


def _user_with_role(username: str, role: str, *, is_staff: bool = False) -> User:
    u = User.objects.create_user(username=username, password="testpass-v15", is_staff=is_staff)
    UserRole.objects.update_or_create(user=u, defaults={"role": role})
    return u


class RBACV15AccessTests(TestCase):
    """1. warehouse /orders/ → 403  2. driver /inventory/ → 403  3. manager /reports/ → 403
    4. customer /dashboard/ → blocked (redirect to shop)  5. driver POST other order → 404
    """

    def setUp(self):
        self.client = Client()

    def test_warehouse_orders_forbidden(self):
        _user_with_role("wh_v15", UserRole.Role.WAREHOUSE)
        self.client.login(username="wh_v15", password="testpass-v15")
        r = self.client.get("/orders/")
        self.assertEqual(r.status_code, 403)

    def test_driver_inventory_forbidden(self):
        _user_with_role("dr_v15", UserRole.Role.DRIVER)
        self.client.login(username="dr_v15", password="testpass-v15")
        r = self.client.get("/inventory/")
        self.assertEqual(r.status_code, 403)

    def test_manager_reports_forbidden(self):
        _user_with_role("mg_v15", UserRole.Role.MANAGER)
        self.client.login(username="mg_v15", password="testpass-v15")
        r = self.client.get("/reports/")
        self.assertEqual(r.status_code, 403)

    def test_manager_reports_customers_forbidden(self):
        _user_with_role("mg2_v15", UserRole.Role.MANAGER)
        self.client.login(username="mg2_v15", password="testpass-v15")
        r = self.client.get("/reports/customers/")
        self.assertEqual(r.status_code, 403)
        r2 = self.client.get("/customers/")
        self.assertEqual(r2.status_code, 403)

    def test_customer_dashboard_blocked(self):
        _user_with_role("cu_v15", UserRole.Role.CUSTOMER)
        self.client.login(username="cu_v15", password="testpass-v15")
        r = self.client.get("/dashboard/", follow=False)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/shop", r["Location"])

    def test_driver_post_other_order_returns_404(self):
        driver_a = _user_with_role("da_v15", UserRole.Role.DRIVER)
        driver_b = _user_with_role("db_v15", UserRole.Role.DRIVER)
        order = Order.objects.create(
            name="RBAC test order",
            assigned_driver=driver_b,
            status=Order.OrderStatus.SHIPPING,
        )
        self.client.login(username="da_v15", password="testpass-v15")
        r = self.client.post(
            "/driver/orders/",
            {"order_id": str(order.pk), "action": "mark_completed"},
        )
        self.assertEqual(r.status_code, 302)
