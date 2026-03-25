"""
Post-login redirects: get_post_login_redirect (role defaults + allowed ``next`` prefixes).
"""

from urllib.parse import urlparse

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

    def test_manager_next_allowed_orders_preserved(self):
        _user_with_role("mg_ok", UserRole.Role.MANAGER)
        c = Client()
        r = c.post(
            "/login/",
            {
                "username": "mg_ok",
                "password": "pw-delivery-01",
                "next": "/orders/?tab=1",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/orders/", r["Location"])

    def test_manager_next_disallowed_shop_ignored(self):
        _user_with_role("mg_shop", UserRole.Role.MANAGER)
        c = Client()
        r = c.post(
            "/login/",
            {
                "username": "mg_shop",
                "password": "pw-delivery-01",
                "next": "/shop/orders/",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/orders/", r["Location"])
        self.assertNotIn("/shop/", r["Location"])

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

    def test_customer_next_checkout_allowed(self):
        _user_with_role("cu_ch", UserRole.Role.CUSTOMER)
        c = Client()
        r = c.post(
            "/login/",
            {
                "username": "cu_ch",
                "password": "pw-delivery-01",
                "next": "/checkout/",
            },
        )
        self.assertEqual(r.status_code, 302)
        self.assertIn("/checkout/", r["Location"])

    def test_driver_next_disallowed_orders_uses_default(self):
        _user_with_role("dr_bad", UserRole.Role.DRIVER)
        c = Client()
        r = c.post(
            "/login/",
            {
                "username": "dr_bad",
                "password": "pw-delivery-01",
                "next": "/orders/",
            },
        )
        self.assertEqual(r.status_code, 302)
        loc_path = urlparse(r["Location"]).path
        self.assertTrue(loc_path.startswith("/driver/"))
        self.assertFalse(loc_path == "/orders" or loc_path.startswith("/orders/"))

    def test_staff_no_userrole_goes_dashboard(self):
        User.objects.create_user(username="st_norole", password="pw-staff", is_staff=True)
        c = Client()
        r = c.post("/login/", {"username": "st_norole", "password": "pw-staff"})
        self.assertEqual(r.status_code, 302)
        self.assertIn("/dashboard/", r["Location"])
        self.assertFalse(UserRole.objects.filter(user__username="st_norole").exists())

    def test_login_preserves_non_customer_role(self):
        """Regression: login must not overwrite UserRole to customer every time."""
        u = _user_with_role("wh_keep", UserRole.Role.WAREHOUSE)
        c = Client()
        c.post("/login/", {"username": "wh_keep", "password": "pw-delivery-01"})
        u.role_profile.refresh_from_db()
        self.assertEqual(u.role_profile.role, UserRole.Role.WAREHOUSE)
