from django.contrib import admin
from import_export.admin import ImportExportModelAdmin

from .models import (
    Customer,
    CustomerProductPrice,
    Ingredient,
    Order,
    OrderItem,
    Product,
    ProductCategory,
    StockLog,
)
from .resources import ProductCategoryResource, ProductResource


class CustomerProductPriceInline(admin.TabularInline):
    model = CustomerProductPrice
    extra = 0
    autocomplete_fields = ("product",)
    fields = ("product", "custom_price_single", "custom_price_case", "is_active")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "phone",
        "account_status",
        "customer_level",
        "allow_credit",
        "credit_limit",
        "payment_cycle",
        "delivery_zone",
        "is_monthly_settlement",
    )
    search_fields = ("name", "phone", "address", "delivery_zone")
    list_filter = (
        "account_status",
        "customer_level",
        "allow_credit",
        "payment_cycle",
        "is_monthly_settlement",
        "delivery_zone",
    )
    fields = (
        "name",
        "phone",
        "account_status",
        "address",
        "delivery_zone",
        "customer_level",
        "allow_credit",
        "credit_limit",
        "payment_cycle",
        "is_monthly_settlement",
        "note",
    )
    inlines = (CustomerProductPriceInline,)


@admin.register(Ingredient)
class IngredientAdmin(admin.ModelAdmin):
    list_display = ("name", "price", "cost_price", "stock", "unit", "warning_level")
    search_fields = ("name", "unit")
    list_filter = ("unit",)


@admin.register(CustomerProductPrice)
class CustomerProductPriceAdmin(admin.ModelAdmin):
    list_display = ("customer", "product", "custom_price_single", "custom_price_case", "is_active")
    list_filter = ("is_active", "customer")
    search_fields = ("customer__name", "product__name", "product__sku")
    autocomplete_fields = ("customer", "product")


@admin.register(ProductCategory)
class ProductCategoryAdmin(ImportExportModelAdmin):
    resource_class = ProductCategoryResource
    list_display = ("name", "sort_order", "is_active")
    list_editable = ("sort_order", "is_active")
    search_fields = ("name",)


@admin.register(Product)
class ProductAdmin(ImportExportModelAdmin):
    resource_class = ProductResource
    list_display = (
        "category",
        "name",
        "sku",
        "image",
        "unit_label",
        "price_single",
        "price_case",
        "stock_quantity",
        "is_active",
    )
    list_filter = ("category", "is_active")
    search_fields = ("name", "sku", "unit_label")
    list_editable = ("is_active", "stock_quantity")


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "customer",
        "workflow_status",
        "stock_deducted",
        "status",
        "total_revenue",
        "total_cost",
        "profit",
        "created_at",
    )
    search_fields = ("name", "customer__name", "delivery_phone", "store_name", "contact_name")
    list_filter = ("workflow_status", "status", "stock_deducted", "created_at")
    list_editable = ("workflow_status", "status")
    readonly_fields = ("stock_deducted", "total_revenue", "total_cost", "profit", "created_at")


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product", "quantity", "sale_type", "unit_price", "pricing_note", "total_revenue", "profit")
    search_fields = ("order__name", "product__name", "product__sku", "pricing_note")


@admin.register(StockLog)
class StockLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "direction", "quantity", "product", "ingredient", "order", "remark")
    list_filter = ("direction", "created_at")
    search_fields = ("product__sku", "product__name", "ingredient__name", "remark")
    readonly_fields = ("created_at", "direction", "quantity", "product", "ingredient", "order", "remark")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
