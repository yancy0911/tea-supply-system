import logging
import csv
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.contrib import admin
from django.contrib import messages
from django import forms
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.shortcuts import render, redirect
from django.urls import path, reverse
from import_export.admin import ImportExportModelAdmin
from django.utils.html import format_html
from django.utils import timezone
from django.db import transaction

from .credit_debt import reverse_credit_debt_if_counted
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


def import_product_costs_csv(file_obj):
    def q2(v):
        return Decimal(str(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    result = {
        "updated_count": 0,
        "skipped_count": 0,
        "missing_skus": [],
        "error_rows": [],
    }
    file_obj.seek(0)
    text = file_obj.read().decode("utf-8-sig")
    reader = csv.DictReader(text.splitlines())
    required = {"sku", "cost_price_single", "cost_price_case"}
    if set(reader.fieldnames or []) != required:
        raise ValidationError("CSV 表头必须为：sku,cost_price_single,cost_price_case")

    for idx, row in enumerate(reader, start=2):
        sku = (row.get("sku") or "").strip()
        if not sku:
            result["error_rows"].append((idx, "sku 为空"))
            continue
        product = Product.objects.filter(sku=sku).first()
        if not product:
            result["missing_skus"].append((idx, sku))
            continue

        raw_single = (row.get("cost_price_single") or "").strip()
        raw_case = (row.get("cost_price_case") or "").strip()
        update_fields = []
        try:
            if raw_single != "":
                product.cost_price_single = float(q2(raw_single))
                update_fields.append("cost_price_single")
            if raw_case != "":
                product.cost_price_case = float(q2(raw_case))
                update_fields.append("cost_price_case")
        except (InvalidOperation, ValueError):
            result["error_rows"].append((idx, f"成本价不是有效数字（sku={sku}）"))
            continue

        if not update_fields:
            result["skipped_count"] += 1
            continue
        product.save(update_fields=update_fields)
        result["updated_count"] += 1

    return result


class ProductAdminForm(forms.ModelForm):
    class Meta:
        model = Product
        fields = "__all__"

    def clean(self):
        cleaned = super().clean()
        cs = cleaned.get("cost_price_single")
        cc = cleaned.get("cost_price_case")
        if cs is None or float(cs) <= 0:
            raise ValidationError("单品成本价必须大于 0，未填写或为 0 时禁止保存。")
        if cc is None or float(cc) <= 0:
            raise ValidationError("整箱成本价必须大于 0，未填写或为 0 时禁止保存。")
        return cleaned


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
        "is_blocked",
        "current_debt_display",
        "credit_limit_display",
        "risk_status_display",
        "account_status",
        "level",
        "allow_credit",
        "minimum_order_amount_display",
    )
    list_display_links = ("shop_name", "name")
    list_editable = ("is_blocked",)
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

    @admin.display(description="风险状态")
    def risk_status_display(self, obj):
        debt = float(obj.current_debt or 0)
        limit = float(obj.credit_limit or 0)
        if obj.is_blocked:
            return format_html('<span style="color:#b91c1c;font-weight:700;">已停单</span>')
        if limit > 0 and debt >= limit:
            return format_html('<span style="color:#b91c1c;font-weight:700;">超额度</span>')
        if limit > 0 and debt / limit >= 0.8:
            return format_html('<span style="color:#ea580c;font-weight:700;">关注</span>')
        return format_html('<span style="color:#15803d;">正常</span>')


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
    form = ProductAdminForm
    exclude = ("image",)
    list_display = (
        "category",
        "name",
        "sku",
        "image_preview",
        "unit_label",
        "price_single",
        "cost_price_single",
        "profit_single_display",
        "profit_rate_single_display",
        "price_case",
        "cost_price_case",
        "profit_case_display",
        "profit_rate_case_display",
        "current_stock",
        "safety_stock",
        "stock_enabled",
        "stock_quantity",
        "is_active",
    )
    list_filter = ("category", "is_active")
    search_fields = ("name", "sku", "unit_label")
    change_list_template = "admin/tea_supply/product/change_list.html"
    readonly_fields = (
        "profit_single_display",
        "profit_rate_single_display",
        "profit_case_display",
        "profit_rate_case_display",
    )

    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path(
                "import-costs/",
                self.admin_site.admin_view(self.import_costs_view),
                name="tea_supply_product_import_costs",
            ),
            path(
                "import-costs/template.csv",
                self.admin_site.admin_view(self.download_cost_template_view),
                name="tea_supply_product_import_costs_template",
            ),
        ]
        return custom + urls

    def import_costs_view(self, request):
        ctx = {
            **self.admin_site.each_context(request),
            "title": "成本价导入",
            "result": None,
            "import_error": "",
            "header_line": "sku,cost_price_single,cost_price_case",
            "template_url": reverse("admin:tea_supply_product_import_costs_template"),
        }
        if request.method == "POST":
            f = request.FILES.get("csv_file")
            if not f:
                ctx["import_error"] = "请先选择 CSV 文件"
                return render(request, "admin/tea_supply/product/import_costs.html", ctx)
            try:
                result = import_product_costs_csv(f)
                ctx["result"] = result
                messages.success(request, "成本价导入完成")
            except Exception as exc:
                ctx["import_error"] = str(exc)
        return render(request, "admin/tea_supply/product/import_costs.html", ctx)

    def download_cost_template_view(self, request):
        content = "sku,cost_price_single,cost_price_case\n"
        resp = HttpResponse(content, content_type="text/csv; charset=utf-8")
        resp["Content-Disposition"] = 'attachment; filename="product_cost_template.csv"'
        return resp

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

    @admin.display(description="单品利润", ordering="price_single")
    def profit_single_display(self, obj: Product):
        cost = getattr(obj, "cost_price_single", None)
        if cost is None or float(cost) <= 0:
            return "-"
        profit = money_float(float(obj.price_single or 0.0) - float(cost or 0.0))
        return f"{profit:.2f}"

    @admin.display(description="单品利润率", ordering="price_single")
    def profit_rate_single_display(self, obj: Product):
        cost = getattr(obj, "cost_price_single", None)
        price = float(obj.price_single or 0.0)
        if cost is None or float(cost) <= 0 or price <= 0:
            return "-"
        profit = float(obj.price_single or 0.0) - float(cost or 0.0)
        rate = (profit / price) * 100.0
        return f"{rate:.2f}%"

    @admin.display(description="整箱利润", ordering="price_case")
    def profit_case_display(self, obj: Product):
        cost = getattr(obj, "cost_price_case", None)
        price = getattr(obj, "price_case", None)
        if cost is None or float(cost) <= 0 or price is None or float(price) <= 0:
            return "-"
        profit = money_float(float(price or 0.0) - float(cost or 0.0))
        return f"{profit:.2f}"

    @admin.display(description="整箱利润率", ordering="price_case")
    def profit_rate_case_display(self, obj: Product):
        cost = getattr(obj, "cost_price_case", None)
        price = float(obj.price_case or 0.0)
        if cost is None or float(cost) <= 0 or price <= 0:
            return "-"
        profit = float(obj.price_case or 0.0) - float(cost or 0.0)
        rate = (profit / price) * 100.0
        return f"{rate:.2f}%"


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
        "calc_total_cost_display",
        "calc_profit_display",
        "profit_rate_display",
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
        "total_revenue_display",
        "calc_total_cost_display",
        "calc_profit_display",
        "profit_rate_display",
        "created_at",
    )
    readonly_fields = (
        "stock_deducted",
        "total_revenue_display",
        "calc_total_cost_display",
        "calc_profit_display",
        "profit_rate_display",
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
        for oid in queryset.values_list("pk", flat=True):
            with transaction.atomic():
                o = Order.objects.select_for_update().get(pk=oid)
                if o.status == Order.Status.PAID:
                    continue
                o.status = Order.Status.PAID
                o.payment_status = Order.PaymentStatus.PAID
                o.paid_at = now
                o.save(update_fields=["status", "payment_status", "paid_at"])
                reverse_credit_debt_if_counted(o)

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

    def _calc_order_cost_profit(self, obj):
        order_amount = money_float(obj.total_revenue or 0)
        total_cost = 0.0
        for item in obj.items.select_related("product").all():
            p = item.product
            qty = float(item.quantity or 0)
            if str(item.sale_type) == OrderItem.SaleType.CASE:
                unit_cost = float(getattr(p, "cost_price_case", 0) or 0)
            else:
                unit_cost = float(getattr(p, "cost_price_single", 0) or 0)
            total_cost += unit_cost * qty
        total_cost = money_float(total_cost)
        total_profit = money_float(order_amount - total_cost)
        profit_rate = (total_profit / order_amount * 100.0) if order_amount > 0 else None
        return order_amount, total_cost, total_profit, profit_rate

    @admin.display(description="总收入", ordering="total_revenue")
    def total_revenue_display(self, obj):
        return f"{money_float(obj.total_revenue or 0):.2f}"

    @admin.display(description="总成本")
    def calc_total_cost_display(self, obj):
        _, total_cost, _, _ = self._calc_order_cost_profit(obj)
        return f"{total_cost:.2f}"

    @admin.display(description="总利润")
    def calc_profit_display(self, obj):
        _, _, total_profit, _ = self._calc_order_cost_profit(obj)
        if total_profit > 0:
            color = "#15803d"  # green
        elif total_profit < 0:
            color = "#b91c1c"  # red
        else:
            color = "#6b7280"  # gray
        return format_html('<span style="color:{};font-weight:700;">{:.2f}</span>', color, total_profit)

    @admin.display(description="利润率")
    def profit_rate_display(self, obj):
        _, _, _, rate = self._calc_order_cost_profit(obj)
        if rate is None:
            return "-"
        if rate >= 30:
            color = "#15803d"  # green
        elif rate >= 10:
            color = "#ea580c"  # orange
        else:
            color = "#b91c1c"  # red
        return format_html('<span style="color:{};font-weight:700;">{:.2f}%</span>', color, rate)


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
