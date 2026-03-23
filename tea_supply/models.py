import threading

from django.core.exceptions import ValidationError
from django.db import transaction, models
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver

# 防止「程序改库存」与「手工改库存」重复记流水
_tls = threading.local()


def _apply_depth_inc():
    _tls.apply_depth = getattr(_tls, "apply_depth", 0) + 1


def _apply_depth_dec():
    _tls.apply_depth = max(0, getattr(_tls, "apply_depth", 0) - 1)


class Ingredient(models.Model):
    name = models.CharField(max_length=100, verbose_name="原材料名称")
    stock = models.FloatField(default=0, verbose_name="当前库存")
    unit = models.CharField(max_length=20, verbose_name="单位（kg / L / 个）")
    warning_level = models.FloatField(default=0, verbose_name="预警值")
    price = models.FloatField(default=0, verbose_name="单价")
    cost_price = models.FloatField(default=0, verbose_name="成本价")

    def __str__(self):
        return self.name


class ProductCategory(models.Model):
    name = models.CharField(max_length=100, verbose_name="分类名称")
    sort_order = models.IntegerField(default=0, verbose_name="排序")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        ordering = ("sort_order", "id")

    def __str__(self):
        return self.name


class Product(models.Model):
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.PROTECT,
        related_name="products",
        verbose_name="分类",
    )
    name = models.CharField(max_length=200, verbose_name="商品名称")
    sku = models.CharField(max_length=64, unique=True, verbose_name="SKU")
    stock_quantity = models.FloatField(
        default=0,
        verbose_name="可售库存",
        help_text="未关联原材料时按此库存扣减；关联原材料则以原材料库存为准。",
    )
    unit_label = models.CharField(max_length=120, blank=True, default="", verbose_name="单位规格")
    case_label = models.CharField(max_length=120, blank=True, default="", verbose_name="整箱规格")
    price_single = models.FloatField(default=0, verbose_name="单品价")
    price_case = models.FloatField(default=0, verbose_name="整箱价")
    cost_price_single = models.FloatField(default=0, verbose_name="单品成本")
    cost_price_case = models.FloatField(default=0, verbose_name="整箱成本")
    shelf_life_months = models.PositiveSmallIntegerField(default=12, verbose_name="保质期（月）")
    can_split_sale = models.BooleanField(default=True, verbose_name="是否可拆卖")
    minimum_order_qty = models.FloatField(default=0.01, verbose_name="起订量")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="关联原材料（库存扣减）",
        help_text="可选；关联后按单品数量或整箱×每箱扣减数扣库存。",
    )
    units_per_case = models.FloatField(
        default=1,
        verbose_name="整箱对应库存扣减数量",
        help_text="整箱下单时：扣减库存 = 数量 × 本字段；单品下单时：扣减数量 = 下单数量。",
    )
    image = models.CharField(
        max_length=500,
        blank=True,
        default="",
        verbose_name="商品主图",
        help_text="相对 MEDIA_ROOT 的路径，如 products/T010103.png；由目录 PDF 导入时写入。",
    )
    catalog_upload = models.ImageField(
        upload_to="products/uploaded/",
        blank=True,
        null=True,
        verbose_name="上传主图",
        help_text="若上传则优先于上方「相对路径」在商城展示；可与 CSV 路径并存。",
    )
    price_on_request = models.BooleanField(
        default=False,
        verbose_name="询价商品",
        help_text="单品价或整箱价任一侧 ≤0 时自动为 True；商城仅展示「联系下单」，不可加入购物车。",
    )

    class Meta:
        ordering = ("category", "name")

    def save(self, *args, **kwargs):
        ul = (self.unit_label or "").strip()
        self.unit_label = ul if ul else "per unit"
        cl = (self.case_label or "").strip()
        self.case_label = cl if cl else "per case"
        ps = float(self.price_single or 0)
        pc = float(self.price_case or 0)
        self.price_on_request = (ps <= 0) or (pc <= 0)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.sku})"


class CustomerProductPrice(models.Model):
    customer = models.ForeignKey(
        "Customer",
        on_delete=models.CASCADE,
        related_name="product_prices",
        verbose_name="客户",
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
        related_name="customer_prices",
        verbose_name="商品",
    )
    custom_price_single = models.FloatField(default=0, verbose_name="专属单品价")
    custom_price_case = models.FloatField(default=0, verbose_name="专属整箱价")
    is_active = models.BooleanField(default=True, verbose_name="是否启用")

    class Meta:
        verbose_name = "客户商品专属价"
        verbose_name_plural = "客户商品专属价"
        unique_together = ("customer", "product")

    def __str__(self):
        return f"{self.customer} — {self.product.sku}"


class Customer(models.Model):
    class AccountStatus(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        DISABLED = "disabled", "已禁用"

    class Level(models.TextChoices):
        C = "C", "C"
        B = "B", "B"
        A = "A", "A"
        VIP = "VIP", "VIP"

    class PaymentCycle(models.TextChoices):
        CASH = "现结", "现结"
        WEEK = "周结", "周结"
        HALF_MONTH = "半月结", "半月结"
        MONTH = "月结", "月结"

    name = models.CharField(max_length=100, verbose_name="客户名称")
    phone = models.CharField(max_length=30, verbose_name="电话")
    account_status = models.CharField(
        max_length=16,
        choices=AccountStatus.choices,
        default=AccountStatus.APPROVED,
        verbose_name="商城账号状态",
        help_text="仅「已通过」可在商城下单；新手机号自助注册为待审核。",
    )
    address = models.CharField(max_length=255, verbose_name="地址")
    delivery_zone = models.CharField(max_length=100, verbose_name="配送区域")
    is_monthly_settlement = models.BooleanField(default=False, verbose_name="是否月结")
    note = models.TextField(blank=True, null=True, verbose_name="备注")
    customer_level = models.CharField(
        max_length=10,
        choices=Level.choices,
        default=Level.C,
        verbose_name="客户等级（VIP）",
        help_text="商城/录单：专属价优先；仅 VIP 享 9 折，其余等级按原价。可在此手动改等级；订单也会按消费自动重算等级。",
    )
    allow_credit = models.BooleanField(
        default=False,
        verbose_name="是否允许欠款",
        help_text="与等级联动自动更新；录单/结算后系统会按规则覆盖手动修改。",
    )
    credit_limit = models.FloatField(
        default=0,
        verbose_name="信用额度",
        help_text="与等级联动自动更新（如 B=100、A=500、VIP=2000）；录单/结算后会覆盖手动修改。",
    )
    payment_cycle = models.CharField(
        max_length=20,
        choices=PaymentCycle.choices,
        default=PaymentCycle.CASH,
        verbose_name="结算周期",
    )

    def __str__(self):
        return self.name

    def shop_order_denial_reason(self):
        """商城下单被拒绝时的说明；空字符串表示可下单。"""
        if self.account_status == self.AccountStatus.PENDING:
            return "账号审核中，请联系店家开通采购权限"
        if self.account_status == self.AccountStatus.DISABLED:
            return "账号已禁用，无法下单，请联系店家"
        return ""


class StockLog(models.Model):
    """库存流水：出库/入库留痕，供供应链对账。"""

    class Direction(models.TextChoices):
        OUT = "OUT", "出库"
        IN = "IN", "入库"

    product = models.ForeignKey(
        "Product",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_logs",
        verbose_name="商品",
    )
    ingredient = models.ForeignKey(
        Ingredient,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="stock_logs",
        verbose_name="原材料",
    )
    order = models.ForeignKey(
        "Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="stock_logs",
        verbose_name="关联订单",
    )
    direction = models.CharField(max_length=3, choices=Direction.choices, verbose_name="方向")
    quantity = models.FloatField(verbose_name="数量")
    remark = models.CharField(max_length=240, blank=True, default="", verbose_name="备注")

    created_at = models.DateTimeField(auto_now_add=True, verbose_name="时间")

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "库存流水"
        verbose_name_plural = "库存流水"

    def __str__(self):
        if self.product_id:
            return f"{self.get_direction_display()} {self.quantity} · {self.product.sku}"
        if self.ingredient_id:
            return f"{self.get_direction_display()} {self.quantity} · {self.ingredient.name}"
        return f"{self.get_direction_display()} {self.quantity}"


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        PAID = "paid", "已结算"

    class WorkflowStatus(models.TextChoices):
        PENDING_CONFIRM = "pending_confirm", "待确认"
        PREPARING = "preparing", "备货中"
        SHIPPED = "shipped", "已发货"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"

    name = models.CharField(max_length=100, default="批发订单", verbose_name="订单名称")
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="客户",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name="结算状态",
    )
    workflow_status = models.CharField(
        max_length=24,
        choices=WorkflowStatus.choices,
        default=WorkflowStatus.PENDING_CONFIRM,
        verbose_name="履约状态",
    )
    contact_name = models.CharField(max_length=100, blank=True, default="", verbose_name="收货人")
    delivery_phone = models.CharField(max_length=30, blank=True, default="", verbose_name="联系电话")
    store_name = models.CharField(max_length=200, blank=True, default="", verbose_name="门店/公司")
    delivery_address = models.CharField(max_length=500, blank=True, default="", verbose_name="配送地址")
    order_note = models.TextField(blank=True, default="", verbose_name="订单备注")
    stock_deducted = models.BooleanField(
        default=False,
        verbose_name="已扣库存",
        help_text="进入「备货中」时扣减；取消或退回待确认时恢复。勿手动改，除非清楚含义。",
    )
    total_revenue = models.FloatField(default=0, verbose_name="总收入")
    total_cost = models.FloatField(default=0, verbose_name="总成本")
    profit = models.FloatField(default=0, verbose_name="利润")

    def __str__(self):
        return self.name


def _stock_need_for_line(order_item, product=None):
    """
    计算本行应扣减的库存数量（与 save/delete 一致）。
    单品：扣减数量 = 下单数量；整箱：扣减 = 数量 × units_per_case。
    关联原材料时扣原材料库存；否则扣商品可售库存 stock_quantity。
    """
    p = product or order_item.product
    q = float(order_item.quantity)
    if order_item.sale_type == OrderItem.SaleType.CASE:
        return q * float(p.units_per_case)
    return q


def recalculate_order_totals(order_id):
    if not order_id:
        return
    order = Order.objects.filter(pk=order_id).first()
    if not order:
        return
    total_revenue = 0.0
    total_cost = 0.0
    for item in order.items.select_related("product").all():
        total_revenue += float(item.total_revenue)
        total_cost += float(item.total_cost)
    profit = total_revenue - total_cost
    Order.objects.filter(pk=order_id).update(
        total_revenue=total_revenue,
        total_cost=total_cost,
        profit=profit,
    )


def assert_order_fits_available_stock(order_id):
    """未扣库存订单：按商品汇总需求量，校验不超过当前库存（下单/改明细时用）。"""
    order = Order.objects.filter(pk=order_id).first()
    if not order or order.stock_deducted:
        return
    needs = {}
    for item in OrderItem.objects.filter(order_id=order_id).select_related("product"):
        pid = item.product_id
        need = _stock_need_for_line(item, item.product)
        needs[pid] = needs.get(pid, 0.0) + float(need)
    for pid, need in needs.items():
        if need <= 0:
            continue
        p = Product.objects.get(pk=pid)
        if p.ingredient_id:
            ing = Ingredient.objects.get(pk=p.ingredient_id)
            if float(ing.stock) < need:
                raise ValidationError(
                    f"商品「{p.name}」库存不足：本单需 {need:g}，当前可售 {float(ing.stock):g}"
                )
        else:
            if float(p.stock_quantity) < need:
                raise ValidationError(
                    f"商品「{p.name}」库存不足：本单需 {need:g}，当前可售 {float(p.stock_quantity):g}"
                )


def _write_stock_log(product, need, *, add_back, order=None, remark=""):
    """写入库存流水（与实物库存变动一致）。"""
    if need <= 0:
        return
    direction = StockLog.Direction.IN if add_back else StockLog.Direction.OUT
    if not remark:
        if order is not None:
            remark = "订单回库" if add_back else "备货出库"
        else:
            remark = "库存入库" if add_back else "库存扣减"
    if product.ingredient_id:
        remark = (remark + "·原材料")[:240]
    StockLog.objects.create(
        product=product,
        order=order,
        direction=direction,
        quantity=float(need),
        remark=remark[:240],
    )


def _apply_need_to_inventory(product, need, *, add_back=False, order=None):
    """
    add_back=False：从库存扣减 need；add_back=True：加回 need。
    order：有则记入流水关联订单。
    """
    if need <= 0:
        return
    _apply_depth_inc()
    try:
        # 浮点库存可能出现极小精度误差；这里做防负库存的兜底处理。
        eps = 1e-9
        if product.ingredient_id:
            ing = Ingredient.objects.select_for_update().get(pk=product.ingredient_id)
            cur = float(ing.stock)
            if add_back:
                new_stock = cur + need
            else:
                if cur + eps < need:
                    raise ValidationError(
                        f"库存不足：商品「{product.name}」（原材料 {ing.name}）现有 {cur:g}，本次需 {need:g}"
                    )
                new_stock = cur - need
            ing.stock = max(0.0, float(new_stock))
            ing.save(update_fields=["stock"])
        else:
            p2 = Product.objects.select_for_update().get(pk=product.pk)
            cur = float(p2.stock_quantity)
            if add_back:
                new_stock = cur + need
            else:
                if cur + eps < need:
                    raise ValidationError(
                        f"库存不足：商品「{product.name}」现有 {cur:g}，本次需 {need:g}"
                    )
                new_stock = cur - need
            p2.stock_quantity = max(0.0, float(new_stock))
            p2.save(update_fields=["stock_quantity"])
        _write_stock_log(product, need, add_back=add_back, order=order)
    finally:
        _apply_depth_dec()


def deduct_stock_for_order(order_id):
    """进入备货：一次性扣减订单全部明细（幂等）。"""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if order.stock_deducted:
            return
        items = list(order.items.select_related("product").all())
        if not items:
            Order.objects.filter(pk=order_id).update(stock_deducted=True)
            return
        for item in items:
            p = item.product
            need = _stock_need_for_line(item, p)
            _apply_need_to_inventory(p, need, add_back=False, order=order)
        Order.objects.filter(pk=order_id).update(stock_deducted=True)


def release_stock_for_order(order_id):
    """取消备货或取消订单：恢复本单已扣库存（幂等）。"""
    with transaction.atomic():
        order = Order.objects.select_for_update().get(pk=order_id)
        if not order.stock_deducted:
            return
        for item in order.items.select_related("product").all():
            p = item.product
            need = _stock_need_for_line(item, p)
            _apply_need_to_inventory(p, need, add_back=True, order=order)
        Order.objects.filter(pk=order_id).update(stock_deducted=False)


class OrderItem(models.Model):
    class SaleType(models.TextChoices):
        SINGLE = "single", "单品"
        CASE = "case", "整箱"

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="items")
    product = models.ForeignKey(Product, on_delete=models.CASCADE, verbose_name="商品")
    quantity = models.FloatField(verbose_name="数量")
    sale_type = models.CharField(
        max_length=10,
        choices=SaleType.choices,
        default=SaleType.SINGLE,
        verbose_name="销售方式",
    )
    unit_price = models.FloatField(default=0, verbose_name="成交单价")
    total_revenue = models.FloatField(default=0, verbose_name="行收入")
    total_cost = models.FloatField(default=0, verbose_name="行成本")
    profit = models.FloatField(default=0, verbose_name="行利润")
    pricing_note = models.CharField(max_length=64, blank=True, default="", verbose_name="折扣来源")

    def __str__(self):
        return f"{self.order.name} - {self.product.name}"

    def _apply_line_amounts(self, p, customer):
        unit_price, pricing_note = resolve_selling_unit_price(customer, p, self.sale_type)
        self.unit_price = float(unit_price)
        self.pricing_note = pricing_note
        q = float(self.quantity)
        self.total_revenue = q * self.unit_price
        if self.sale_type == self.SaleType.CASE:
            cpu = float(p.cost_price_case)
        else:
            cpu = float(p.cost_price_single)
        self.total_cost = q * cpu
        self.profit = self.total_revenue - self.total_cost

    def save(self, *args, **kwargs):
        if self.quantity <= 0:
            raise ValidationError("数量必须大于 0")

        creating = self.pk is None

        with transaction.atomic():
            order = Order.objects.select_related("customer").get(pk=self.order_id)
            if not order.customer_id:
                raise ValidationError("订单必须指定客户后才能计算价格")
            customer = order.customer

            p = Product.objects.select_for_update().get(pk=self.product_id)
            if not p.is_active:
                raise ValidationError("该商品已停用，无法下单")
            if self.sale_type == self.SaleType.SINGLE and not p.can_split_sale:
                raise ValidationError("该商品不可拆卖，请选择整箱")
            if float(self.quantity) < float(p.minimum_order_qty):
                raise ValidationError(f"数量不能低于起订量 {p.minimum_order_qty}")

            self._apply_line_amounts(p, customer)

            if order.stock_deducted:
                if creating:
                    need = _stock_need_for_line(self, p)
                    _apply_need_to_inventory(p, need, add_back=False, order=order)
                    super().save(*args, **kwargs)
                    recalculate_order_totals(self.order_id)
                    return

                old_item = (
                    OrderItem.objects.select_for_update()
                    .select_related("product")
                    .get(pk=self.pk)
                )
                old_need = _stock_need_for_line(old_item, old_item.product)
                new_need = _stock_need_for_line(self, p)

                if old_item.product_id == self.product_id and old_item.sale_type == self.sale_type:
                    diff = new_need - old_need
                    if diff > 0:
                        _apply_need_to_inventory(p, diff, add_back=False, order=order)
                    elif diff < 0:
                        _apply_need_to_inventory(p, -diff, add_back=True, order=order)
                else:
                    if old_need > 0:
                        _apply_need_to_inventory(old_item.product, old_need, add_back=True, order=order)
                    if new_need > 0:
                        _apply_need_to_inventory(p, new_need, add_back=False, order=order)

                super().save(*args, **kwargs)
                recalculate_order_totals(self.order_id)
                return

            if creating:
                super().save(*args, **kwargs)
                recalculate_order_totals(self.order_id)
                assert_order_fits_available_stock(self.order_id)
                return

            super().save(*args, **kwargs)
            recalculate_order_totals(self.order_id)
            assert_order_fits_available_stock(self.order_id)

    def delete(self, *args, **kwargs):
        oid = self.order_id
        with transaction.atomic():
            order = Order.objects.select_for_update().get(pk=self.order_id)
            if order.stock_deducted:
                p = Product.objects.select_for_update().get(pk=self.product_id)
                need = _stock_need_for_line(self, p)
                if need > 0:
                    _apply_need_to_inventory(p, need, add_back=True, order=order)
            super().delete(*args, **kwargs)
        recalculate_order_totals(oid)


# 仅 VIP 享折扣；C/B/A 为原价（1.0）。商城与录单共用。
CUSTOMER_TIER_DISCOUNT = {
    Customer.Level.C: 1.0,
    Customer.Level.B: 1.0,
    Customer.Level.A: 1.0,
    Customer.Level.VIP: 0.90,
}


def resolve_selling_unit_price(customer, product, sale_type):
    """
    未登录/无客户：基础价。
    有客户：专属价（>0）优先 → 否则 VIP 9 折 → 否则原价。
    """
    if sale_type == OrderItem.SaleType.CASE:
        base = float(product.price_case)
    else:
        base = float(product.price_single)

    if customer is None:
        return base, "基础价"

    cp = (
        CustomerProductPrice.objects.filter(customer=customer, product=product, is_active=True)
        .only("custom_price_single", "custom_price_case")
        .first()
    )
    if cp:
        if sale_type == OrderItem.SaleType.CASE:
            ex = float(cp.custom_price_case)
        else:
            ex = float(cp.custom_price_single)
        if ex > 0:
            return ex, "专属价"
    tier = customer.customer_level
    d = float(CUSTOMER_TIER_DISCOUNT.get(tier, 1.0))
    if sale_type == OrderItem.SaleType.CASE:
        price = float(product.price_case) * d
    else:
        price = float(product.price_single) * d
    if abs(d - 1.0) < 1e-9:
        note = "原价"
    else:
        note = "VIP 9折"
    return price, note


def total_spent_for_customer(customer):
    total = 0.0
    for item in OrderItem.objects.filter(order__customer=customer).select_related("product"):
        total += float(item.total_revenue)
    return total


def level_from_total_spent(total_spent):
    if total_spent < 200:
        return Customer.Level.C
    if total_spent < 500:
        return Customer.Level.B
    if total_spent < 1000:
        return Customer.Level.A
    return Customer.Level.VIP


def tier_limits_from_total_spent(total_spent):
    if total_spent < 200:
        return False, 0.0
    if total_spent < 500:
        return True, 100.0
    if total_spent < 1000:
        return True, 500.0
    return True, 2000.0


def update_customer_tier_from_spending(customer_id):
    customer = Customer.objects.get(pk=customer_id)
    total = total_spent_for_customer(customer)
    level = level_from_total_spent(total)
    allow_credit, credit_limit = tier_limits_from_total_spent(total)
    customer.customer_level = level
    customer.allow_credit = allow_credit
    customer.credit_limit = credit_limit
    customer.save(update_fields=["customer_level", "allow_credit", "credit_limit"])


def _should_release_stock_on_workflow(old_wf, new_wf):
    if new_wf == Order.WorkflowStatus.CANCELLED:
        return True
    if new_wf == Order.WorkflowStatus.PENDING_CONFIRM and old_wf == Order.WorkflowStatus.PREPARING:
        return True
    return False


@receiver(pre_save, sender=Order)
def _order_cache_prev_workflow(sender, instance, **kwargs):
    if instance.pk:
        try:
            prev = Order.objects.get(pk=instance.pk)
            instance._prev_workflow_status = prev.workflow_status
            instance._prev_stock_deducted = prev.stock_deducted
        except Order.DoesNotExist:
            instance._prev_workflow_status = None
            instance._prev_stock_deducted = False


@receiver(post_save, sender=Order)
def _order_workflow_inventory(sender, instance, created, **kwargs):
    """履约状态变更：进入备货中扣库存；取消/退回待确认/已取消时恢复库存。"""
    if created:
        return
    old_wf = getattr(instance, "_prev_workflow_status", None)
    if old_wf is None:
        return
    new_wf = instance.workflow_status
    if old_wf == new_wf:
        return
    prev_deducted = getattr(instance, "_prev_stock_deducted", False)
    try:
        if (
            new_wf == Order.WorkflowStatus.PREPARING
            and old_wf != Order.WorkflowStatus.PREPARING
            and not prev_deducted
        ):
            deduct_stock_for_order(instance.pk)
        elif prev_deducted and _should_release_stock_on_workflow(old_wf, new_wf):
            release_stock_for_order(instance.pk)
    except ValidationError:
        Order.objects.filter(pk=instance.pk).update(workflow_status=old_wf)
        raise


@receiver(post_save, sender=OrderItem)
def _order_item_saved_update_tier(sender, instance, **kwargs):
    if instance.order.customer_id:
        update_customer_tier_from_spending(instance.order.customer_id)


@receiver(post_delete, sender=OrderItem)
def _order_item_deleted_update_tier(sender, instance, **kwargs):
    if instance.order.customer_id:
        update_customer_tier_from_spending(instance.order.customer_id)


@receiver(pre_save, sender=Product)
def _product_prev_stock_qty(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = Product.objects.only("stock_quantity").get(pk=instance.pk)
            instance._prev_stock_qty = float(old.stock_quantity)
        except Product.DoesNotExist:
            instance._prev_stock_qty = None
    else:
        instance._prev_stock_qty = None


@receiver(post_save, sender=Product)
def _product_manual_stock_log(sender, instance, created, **kwargs):
    """后台直接改商品可售库存时记流水（与程序扣减区分）。"""
    if created:
        return
    if getattr(_tls, "apply_depth", 0) > 0:
        return
    prev = getattr(instance, "_prev_stock_qty", None)
    if prev is None:
        return
    cur = float(instance.stock_quantity)
    delta = cur - prev
    if abs(delta) < 1e-9:
        return
    if delta > 0:
        StockLog.objects.create(
            product=instance,
            direction=StockLog.Direction.IN,
            quantity=abs(delta),
            remark="手工入库/补货",
        )
    else:
        StockLog.objects.create(
            product=instance,
            direction=StockLog.Direction.OUT,
            quantity=abs(delta),
            remark="手工减少库存",
        )


@receiver(pre_save, sender=Ingredient)
def _ingredient_prev_stock(sender, instance, **kwargs):
    if instance.pk:
        try:
            old = Ingredient.objects.only("stock").get(pk=instance.pk)
            instance._prev_ing_stock = float(old.stock)
        except Ingredient.DoesNotExist:
            instance._prev_ing_stock = None
    else:
        instance._prev_ing_stock = None


@receiver(post_save, sender=Ingredient)
def _ingredient_manual_stock_log(sender, instance, created, **kwargs):
    if created:
        return
    if getattr(_tls, "apply_depth", 0) > 0:
        return
    prev = getattr(instance, "_prev_ing_stock", None)
    if prev is None:
        return
    cur = float(instance.stock)
    delta = cur - prev
    if abs(delta) < 1e-9:
        return
    if delta > 0:
        StockLog.objects.create(
            ingredient=instance,
            direction=StockLog.Direction.IN,
            quantity=abs(delta),
            remark="原材料补货/入库",
        )
    else:
        StockLog.objects.create(
            ingredient=instance,
            direction=StockLog.Direction.OUT,
            quantity=abs(delta),
            remark="原材料手工减少",
        )


@receiver(post_save, sender=Order)
def _order_saved_update_tier(sender, instance, **kwargs):
    if instance.customer_id:
        update_customer_tier_from_spending(instance.customer_id)
