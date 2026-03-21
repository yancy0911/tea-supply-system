from django.urls import path
from tea_supply.views import (
    demo_landing,
    inventory_list,
    mark_order_paid,
    order_status_update,
    orders_list,
    replenishment_dashboard,
    shop_checkout,
    shop_home,
    shop_login,
    shop_logout,
    shop_order_success,
    shop_product_detail,
    shop_submit_order,
    wholesale_order_entry,
)

urlpatterns = [
    path("demo/", demo_landing, name="demo-landing"),
    path("shop/", shop_home, name="shop-home"),
    path("shop/login/", shop_login, name="shop-login"),
    path("shop/logout/", shop_logout, name="shop-logout"),
    path("shop/checkout/", shop_checkout, name="shop-checkout"),
    path("shop/product/<int:product_id>/", shop_product_detail, name="shop-product-detail"),
    path("shop/order/", shop_submit_order, name="shop-submit-order"),
    path("shop/order/success/<int:order_id>/", shop_order_success, name="shop-order-success"),
    path("", wholesale_order_entry, name="wholesale-order-entry"),
    path("inventory/", inventory_list, name="inventory-list"),
    path("replenishment/", replenishment_dashboard, name="replenishment-dashboard"),
    path("orders/", orders_list, name="orders-list"),
    path("order/<int:order_id>/paid/", mark_order_paid, name="order_paid"),
    path("orders/<int:order_id>/status/", order_status_update, name="order-status-update"),
]