from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from django.utils.html import format_html

from .models import (
    Customer,
    CustomerProductPrice,
    Ingredient,
    Order,
    OrderItem,
    Product,
    ProductCategory,
    StockLog,
    UserRole,
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
        "user",
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
    autocomplete_fields = ("user",)


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

    def has_module_permission(self, request):
        return request.user.is_superuser


@admin.register(Product)
class ProductAdmin(ImportExportModelAdmin):
    """list_editable 与 import_export 在部分环境下会导致 Import 按钮不显示，故不在列表内联编辑。"""
    resource_class = ProductResource
    exclude = ("image",)
    list_display = (
        "category",
        "name",
        "sku",
        "image_preview",
        "unit_label",
        "price_single",
        "price_case",
        "stock_quantity",
        "is_active",
    )
    list_filter = ("category", "is_active")
    search_fields = ("name", "sku", "unit_label")

    def has_module_permission(self, request):
        return request.user.is_superuser

    def image_preview(self, obj: Product):
        if not getattr(obj, "catalog_upload", None):
            return "—"
        try:
            url = obj.catalog_upload.url
            return format_html('<img src="{}" style="height:40px; width:auto; object-fit:contain;" />', url)
        except Exception:
            return "—"

    image_preview.short_description = "图片"


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0
    readonly_fields = ("product", "quantity", "sale_type", "unit_price", "total_revenue", "total_cost", "profit")
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    inlines = (OrderItemInline,)
    list_display = (
        "name",
        "customer",
        "ordered_by",
        "guest_session_key",
        "workflow_status",
        "stock_deducted",
        "status",
        "total_revenue",
        "total_cost",
        "profit",
        "created_at",
    )
    search_fields = (
        "name",
        "customer__name",
        "ordered_by__username",
        "delivery_phone",
        "store_name",
        "contact_name",
        "guest_session_key",
    )
    list_filter = ("workflow_status", "status", "stock_deducted", "created_at", "customer", "ordered_by")
    list_editable = ("workflow_status", "status")
    readonly_fields = (
        "stock_deducted",
        "total_revenue",
        "total_cost",
        "profit",
        "created_at",
        "guest_session_key",
        "ordered_by",
    )

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_staff)

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_staff)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_superuser)


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = ("order", "product", "quantity", "sale_type", "unit_price", "pricing_note", "total_revenue", "profit")
    search_fields = ("order__name", "product__name", "product__sku", "pricing_note")

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_staff)

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_staff)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_superuser)


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


@admin.register(UserRole)
class UserRoleAdmin(admin.ModelAdmin):
    list_display = ("user", "role")
    list_filter = ("role",)
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email")
    autocomplete_fields = ("user",)
