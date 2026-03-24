import logging

from django.contrib import admin
from import_export.admin import ImportExportModelAdmin
from django.utils.html import format_html
from django.utils import timezone

from .models import (
    CreditApplication,
    Customer,
    CustomerProductPrice,
    Ingredient,
    InventoryLog,
    Order,
    OrderItem,
    Product,
    ProductCategory,
    StockLog,
    UserRole,
)
from .money_utils import money_float
from .resources import ProductCategoryResource, ProductResource

logger = logging.getLogger(__name__)


class CustomerProductPriceInline(admin.TabularInline):
    model = CustomerProductPrice
    extra = 0
    autocomplete_fields = ("product",)
    fields = ("product", "custom_price_single", "custom_price_case", "is_active")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "shop_name",
        "name",
        "contact_name",
        "phone",
        "current_debt_display",
        "credit_limit_display",
        "is_blocked_display",
        "account_status",
        "level",
        "allow_credit",
        "minimum_order_amount_display",
    )
    list_display_links = ("shop_name", "name")
    search_fields = ("name", "contact_name", "phone", "address", "delivery_zone")
    list_filter = (
        "account_status",
        "customer_level",
        "allow_credit",
        "is_active",
        "is_blocked",
        "payment_cycle",
        "is_monthly_settlement",
        "delivery_zone",
    )
    fields = (
        "user",
        "shop_name",
        "name",
        "contact_name",
        "phone",
        "account_status",
        "address",
        "delivery_zone",
        "customer_level",
        "allow_credit",
        "credit_limit",
        "current_debt",
        "is_blocked",
        "minimum_order_amount",
        "is_active",
        "payment_cycle",
        "is_monthly_settlement",
        "note",
    )
    inlines = (CustomerProductPriceInline,)
    autocomplete_fields = ("user",)

    @admin.display(description="当前欠款", ordering="current_debt")
    def current_debt_display(self, obj):
        return f"{money_float(obj.current_debt or 0):.2f}"

    @admin.display(description="信用额度", ordering="credit_limit")
    def credit_limit_display(self, obj):
        return f"{money_float(obj.credit_limit or 0):.2f}"

    @admin.display(description="起送金额", ordering="minimum_order_amount")
    def minimum_order_amount_display(self, obj):
        return f"{money_float(obj.minimum_order_amount or 0):.2f}"

    @admin.display(description="停单", ordering="is_blocked")
    def is_blocked_display(self, obj):
        if obj.is_blocked:
            return format_html(
                '<span style="color:#c00;font-weight:600;">已停单</span>'
            )
        return format_html('<span style="color:#080;">否</span>')


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
        "current_stock",
        "safety_stock",
        "stock_enabled",
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
        "settlement_type",
        "payment_method",
        "payment_status",
        "stock_deducted",
        "status",
        "stripe_session_id",
        "paid_at",
        "total_revenue_display",
        "total_cost_display",
        "profit_display",
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
    list_filter = (
        "workflow_status",
        "status",
        "settlement_type",
        "payment_method",
        "payment_status",
        "stock_deducted",
        "created_at",
        "customer",
        "ordered_by",
    )
    list_editable = ("workflow_status", "status", "settlement_type", "payment_method", "payment_status")
    fields = (
        "name",
        "customer",
        "ordered_by",
        "guest_session_key",
        "workflow_status",
        "status",
        "settlement_type",
        "payment_method",
        "payment_status",
        "transfer_reference",
        "order_note",
        "paid_at",
        "delivery_phone",
        "contact_name",
        "store_name",
        "delivery_address",
        "stripe_session_id",
        "stock_deducted",
        "total_revenue",
        "total_cost",
        "profit",
        "created_at",
    )
    readonly_fields = (
        "stock_deducted",
        "total_revenue",
        "total_cost",
        "profit",
        "created_at",
        "guest_session_key",
        "ordered_by",
        "stripe_session_id",
    )
    actions = ("action_mark_paid", "action_mark_pending_confirmation", "action_mark_cancelled")

    @admin.action(description="标记已收款")
    def action_mark_paid(self, request, queryset):
        now = timezone.now()
        queryset.update(status=Order.Status.PAID, payment_status=Order.PaymentStatus.PAID, paid_at=now)

    @admin.action(description="标记待确认")
    def action_mark_pending_confirmation(self, request, queryset):
        queryset.update(payment_status=Order.PaymentStatus.PENDING_CONFIRMATION)

    @admin.action(description="标记已取消")
    def action_mark_cancelled(self, request, queryset):
        queryset.update(payment_status=Order.PaymentStatus.CANCELLED)

    def has_view_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_staff)

    def has_change_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_staff)

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_superuser)

    def get_queryset(self, request):
        # 不在此对全表 .distinct()：与 list_editable 同时使用时，部分环境下列表保存无法正确落库。
        return super().get_queryset(request)

    def save_model(self, request, obj, form, change):
        if change and obj.pk:
            try:
                prev = Order.objects.filter(pk=obj.pk).values(
                    "workflow_status",
                    "settlement_type",
                    "payment_method",
                    "payment_status",
                    "status",
                ).first()
            except Exception:
                prev = None
            if prev:
                logger.info(
                    "OrderAdmin.save_model order_id=%s before wf=%s settlement=%s pm=%s ps=%s status=%s",
                    obj.pk,
                    prev["workflow_status"],
                    prev["settlement_type"],
                    prev["payment_method"],
                    prev["payment_status"],
                    prev["status"],
                )
        logger.info(
            "OrderAdmin.save_model order_id=%s entering super().save_model change=%s",
            getattr(obj, "pk", None),
            change,
        )
        try:
            super().save_model(request, obj, form, change)
        except Exception as exc:
            logger.exception(
                "OrderAdmin.save_model order_id=%s failed (not saved): %s",
                getattr(obj, "pk", None),
                exc,
            )
            raise
        logger.info("OrderAdmin.save_model order_id=%s save() completed", obj.pk)

    @admin.display(description="总收入", ordering="total_revenue")
    def total_revenue_display(self, obj):
        return f"{money_float(obj.total_revenue or 0):.2f}"

    @admin.display(description="总成本", ordering="total_cost")
    def total_cost_display(self, obj):
        return f"{money_float(obj.total_cost or 0):.2f}"

    @admin.display(description="利润", ordering="profit")
    def profit_display(self, obj):
        return f"{money_float(obj.profit or 0):.2f}"


@admin.register(OrderItem)
class OrderItemAdmin(admin.ModelAdmin):
    list_display = (
        "order",
        "product",
        "quantity",
        "sale_type",
        "unit_price_display",
        "unit_cost_display",
        "pricing_note",
        "total_revenue_display",
        "total_cost_display",
        "profit_display",
    )
    search_fields = ("order__name", "product__name", "product__sku", "pricing_note")

    @admin.display(description="成交单价", ordering="unit_price")
    def unit_price_display(self, obj):
        return f"{money_float(obj.unit_price or 0):.2f}"

    @admin.display(description="单位成本", ordering="unit_cost")
    def unit_cost_display(self, obj):
        return f"{money_float(obj.unit_cost or 0):.2f}"

    @admin.display(description="行收入", ordering="total_revenue")
    def total_revenue_display(self, obj):
        return f"{money_float(obj.total_revenue or 0):.2f}"

    @admin.display(description="行成本", ordering="total_cost")
    def total_cost_display(self, obj):
        return f"{money_float(obj.total_cost or 0):.2f}"

    @admin.display(description="行利润", ordering="profit")
    def profit_display(self, obj):
        return f"{money_float(obj.profit or 0):.2f}"

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


@admin.register(InventoryLog)
class InventoryLogAdmin(admin.ModelAdmin):
    list_display = ("created_at", "product", "order", "change_type", "quantity", "before_stock", "after_stock", "note")
    list_filter = ("change_type", "created_at")
    search_fields = ("product__sku", "product__name", "note")
    readonly_fields = ("created_at", "product", "order", "change_type", "quantity", "before_stock", "after_stock", "note")

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


@admin.register(CreditApplication)
class CreditApplicationAdmin(admin.ModelAdmin):
    list_display = (
        "customer",
        "shop_name",
        "phone",
        "requested_credit_limit",
        "status",
        "approved_credit_limit",
        "created_at",
        "reviewed_at",
    )
    list_filter = ("status", "created_at", "reviewed_at", "customer")
    search_fields = ("customer__name", "shop_name", "contact_name", "phone")
    list_editable = ("status", "approved_credit_limit")
    readonly_fields = ("created_at", "reviewed_at")

    def has_delete_permission(self, request, obj=None):
        return bool(request.user.is_authenticated and request.user.is_superuser)
