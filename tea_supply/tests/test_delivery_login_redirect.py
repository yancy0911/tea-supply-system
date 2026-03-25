"""
Post-login default redirects and safe ``next`` handling (see rbac.resolve_login_redirect_url).
"""

from django.contrib.auth.models import User
from django.test import Client, TestCase

from tea_supply.models import UserRole


def _user_with_role(username: str, role: str, password: str = "pw-delivery-01") -> User:
    u = User.objects.create_user(username=username, password=password)
    UserRole.objects.update_or_create(user=u, defaults={"role": role})
    return u


class DeliveryLoginRedirectTests(TestCase):
    def test_owner_goes_to_dashboard(self):
        _user_with_role("own_dl", UserRole.Role.OWNER)
        c = Client()
        r = c.post("/login/", {"username": "own_dl", "password": "pw-delivery-01"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/dashboard/", r["Location"])

    def test_manager_goes_to_orders(self):
        _user_with_role("mg_dl", UserRole.Role.MANAGER)
        c = Client()
        r = c.post("/login/", {"username": "mg_dl", "password": "pw-delivery-01"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/orders/", r["Location"])

    def test_warehouse_goes_to_inventory(self):
        _user_with_role("wh_dl", UserRole.Role.WAREHOUSE)
        c = Client()
        r = c.post("/login/", {"username": "wh_dl", "password": "pw-delivery-01"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/inventory/", r["Location"])

    def test_driver_goes_to_driver_orders(self):
        _user_with_role("dr_dl", UserRole.Role.DRIVER)
        c = Client()
        r = c.post("/login/", {"username": "dr_dl", "password": "pw-delivery-01"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/driver/orders/", r["Location"])

    def test_customer_goes_to_shop(self):
        _user_with_role("cu_dl", UserRole.Role.CUSTOMER)
        c = Client()
        r = c.post("/login/", {"username": "cu_dl", "password": "pw-delivery-01"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/shop/", r["Location"])

    def test_safe_next_relative_honored(self):
        _user_with_role("mg_dl2", UserRole.Role.MANAGER)
        c = Client()
        r = c.post(
            "/login/",
            {"username": "mg_dl2", "password": "pw-delivery-01", "next": "/shop/orders/"},
        )
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].endswith("/shop/orders/"))

    def test_unsafe_next_open_redirect_ignored(self):
        _user_with_role("mg_dl3", UserRole.Role.MANAGER)
        c = Client()
        r = c.post(
            "/login/",
            {
                "username": "mg_dl3",
                "password": "pw-delivery-01",
                "next": "//evil.example/phish",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/orders/", r["Location"])

    def test_login_preserves_non_customer_role(self):
        """Regression: login must not overwrite UserRole to customer every time."""
        u = _user_with_role("wh_keep", UserRole.Role.WAREHOUSE)
        c = Client()
        c.post("/login/", {"username": "wh_keep", "password": "pw-delivery-01"})
        u.role_profile.refresh_from_db()
        self.assertEqual(u.role_profile.role, UserRole.Role.WAREHOUSE)
