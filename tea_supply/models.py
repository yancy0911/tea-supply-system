import threading

from django.core.exceptions import ValidationError
from django.contrib.auth.models import User
from django.db import transaction, models
from django.db.models.signals import post_delete, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .money_utils import money_dec, money_float, money_q2

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
    current_stock = models.FloatField(
        default=100,
        verbose_name="当前库存",
        help_text="稳版统一库存（默认假库存 100），用于商城下单校验与扣减。",
    )
    safety_stock = models.FloatField(
        default=10,
        verbose_name="安全库存",
        help_text="低于或等于该值时前台显示低库存提醒。",
    )
    stock_enabled = models.BooleanField(
        default=True,
        verbose_name="启用库存校验",
        help_text="关闭后允许忽略库存校验继续下单。",
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
        null=True,
        blank=True,
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
    contact_name = models.CharField(
        max_length=100,
        blank=True,
        default="",
        verbose_name="联系人",
        help_text="可与客户名称不同；前台可自助修改。",
    )
    phone = models.CharField(max_length=30, verbose_name="电话")
    shop_name = models.CharField(max_length=200, blank=True, default="", verbose_name="店名")
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
        help_text="商城/录单：专属价优先；否则按统一等级折扣（VIP×0.90、A×0.95、B×0.98、C×1.00）乘基础价。可手动改等级；订单也会按消费自动重算等级。",
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
    current_debt = models.FloatField(
        default=0,
        verbose_name="当前欠款",
        help_text="挂账订单累计未收款金额；收款后自动减少，不低于 0。",
    )
    is_blocked = models.BooleanField(
        default=False,
        verbose_name="是否停单",
        help_text="欠款≥信用额度时系统自动停单；后台可手动解除（欠款与额度未变时保留解除状态）。",
    )
    minimum_order_amount = models.FloatField(
        default=0,
        verbose_name="起送金额",
        help_text="商城/录单起送门槛（元）；0 表示不设门槛。",
    )
    is_active = models.BooleanField(
        default=True,
        verbose_name="客户档案启用",
        help_text="关闭后该客户无法在商城下单（与「商城账号状态」独立，供运营停用档案）。",
    )
    payment_cycle = models.CharField(
        max_length=20,
        choices=PaymentCycle.choices,
        default=PaymentCycle.CASH,
        verbose_name="结算周期",
    )
    user = models.OneToOneField(
        User,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="customer_profile",
        verbose_name="登录账号",
        help_text="统一账号体系：客户使用 Django User 登录商城。",
    )

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        old_row = None
        if self.pk:
            old_row = (
                Customer.objects.filter(pk=self.pk)
                .values("is_blocked", "current_debt", "credit_limit")
                .first()
            )
        prev_blocked = bool(old_row["is_blocked"]) if old_row else False
        self._apply_credit_block_rule(old_row)
        if update_fields is not None:
            ufs = list(update_fields)
            if self.is_blocked != prev_blocked and "is_blocked" not in ufs:
                ufs.append("is_blocked")
            kwargs["update_fields"] = ufs
        super().save(*args, **kwargs)

    def _apply_credit_block_rule(self, old_row):
        limit = float(self.credit_limit or 0)
        debt = float(self.current_debt or 0)
        if limit > 0 and debt >= limit:
            if old_row:
                old_debt = float(old_row["current_debt"] or 0)
                old_limit = float(old_row["credit_limit"] or 0)
                debt_same = abs(old_debt - debt) < 1e-6
                limit_same = abs(old_limit - limit) < 1e-6
                if debt_same and limit_same and not self.is_blocked:
                    return
            self.is_blocked = True
        elif limit > 0 and debt < limit:
            self.is_blocked = False

    def shop_order_denial_reason(self):
        """商城下单被拒绝时的说明；空字符串表示可下单。"""
        if self.account_status == self.AccountStatus.PENDING:
            return "账号审核中，请联系店家开通采购权限"
        if self.account_status == self.AccountStatus.DISABLED:
            return "账号已禁用，无法下单，请联系店家"
        if not self.is_active:
            return "客户档案已停用，无法下单，请联系店家"
        if self.is_blocked:
            return "客户已超额度，已暂停下单"
        return ""

    @property
    def level(self):
        """与 customer_level 同义（兼容「等级」字段命名）。"""
        return self.customer_level


class CustomerLevelPriceRule(models.Model):
    """历史表：当前定价已改为代码常量 CUSTOMER_LEVEL_DISCOUNT_RATES，请勿再依赖本表。"""

    level = models.CharField(
        max_length=10,
        choices=Customer.Level.choices,
        unique=True,
        verbose_name="等级",
    )
    single_discount_rate = models.FloatField(
        default=1.0,
        verbose_name="单品折扣率",
        help_text="成交价 = 单品基础价 × 本系数；1.0 为原价。",
    )
    case_discount_rate = models.FloatField(
        default=1.0,
        verbose_name="整箱折扣率",
        help_text="成交价 = 整箱基础价 × 本系数；1.0 为原价。",
    )
    is_active = models.BooleanField(default=True, verbose_name="启用")

    class Meta:
        verbose_name = "等级折扣规则"
        verbose_name_plural = "等级折扣规则"

    def __str__(self):
        return f"{self.level} 单×{self.single_discount_rate} 箱×{self.case_discount_rate}"


# 统一等级折扣（代码常量，单品/整箱同一系数）。实际定价不再读取 CustomerLevelPriceRule 表，避免与后台双源冲突。
CUSTOMER_LEVEL_DISCOUNT_RATES = {
    Customer.Level.VIP: 0.90,
    Customer.Level.A: 0.95,
    Customer.Level.B: 0.98,
    Customer.Level.C: 1.00,
}


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


class InventoryLog(models.Model):
    class ChangeType(models.TextChoices):
        IN = "in", "入库"
        OUT = "out", "出库"
        ADJUST = "adjust", "调整"

    product = models.ForeignKey("Product", on_delete=models.CASCADE, related_name="inventory_logs", verbose_name="商品")
    order = models.ForeignKey(
        "Order",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="inventory_logs",
        verbose_name="关联订单",
    )
    change_type = models.CharField(max_length=12, choices=ChangeType.choices, verbose_name="变动类型")
    quantity = models.FloatField(default=0, verbose_name="变动数量")
    before_stock = models.FloatField(default=0, verbose_name="变动前库存")
    after_stock = models.FloatField(default=0, verbose_name="变动后库存")
    note = models.CharField(max_length=240, blank=True, default="", verbose_name="备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="时间")

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "库存流水（稳版）"
        verbose_name_plural = "库存流水（稳版）"

    def __str__(self):
        return f"{self.product.sku} {self.get_change_type_display()} {self.quantity:g}"


class UserRole(models.Model):
    class Role(models.TextChoices):
        OWNER = "owner", "老板"
        STAFF = "staff", "员工"
        CUSTOMER = "customer", "客户"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="role_profile")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.CUSTOMER, verbose_name="角色")

    class Meta:
        verbose_name = "用户角色"
        verbose_name_plural = "用户角色"

    def __str__(self):
        return f"{self.user.username}({self.get_role_display()})"


class Order(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待处理"
        PAID = "paid", "已结算"

    class WorkflowStatus(models.TextChoices):
        PENDING_CONFIRM = "pending_confirm", "待确认"
        CONFIRMED = "confirmed", "已确认"
        PREPARING = "preparing", "备货中"
        SHIPPED = "shipped", "已发货"
        COMPLETED = "completed", "已完成"
        CANCELLED = "cancelled", "已取消"
    class SettlementType(models.TextChoices):
        CASH = "cash", "现结"
        CREDIT = "credit", "挂账"

    class PaymentMethod(models.TextChoices):
        BANK_TRANSFER = "bank_transfer", "银行转账"
        CHECK = "check", "支票"
        CARD_ON_PICKUP = "card_on_pickup", "现场刷卡/取货付款"
        CASH = "cash", "现金"
        CREDIT = "credit", "挂账"

    class PaymentStatus(models.TextChoices):
        UNPAID = "unpaid", "未支付"
        PENDING_CONFIRMATION = "pending_confirmation", "待确认"
        PAID = "paid", "已支付"
        PARTIAL = "partial", "部分付款"
        CANCELLED = "cancelled", "已取消"

    name = models.CharField(max_length=100, default="批发订单", verbose_name="订单名称")
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        verbose_name="客户",
    )
    ordered_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="orders_created",
        verbose_name="下单用户",
        help_text="提交订单时绑定 request.user（客户前台/员工录单）。",
    )
    guest_session_key = models.CharField(
        max_length=40,
        blank=True,
        default="",
        verbose_name="游客会话标识",
        help_text="未登录商城下单时写入 session key，用于成功页校验；已登录客户订单为空。",
    )
    confirmed = models.BooleanField(
        default=False,
        verbose_name="商家已确认",
        help_text="与履约状态配合使用；新单默认未确认。",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        verbose_name="结算状态",
    )
    settlement_type = models.CharField(
        max_length=12,
        choices=SettlementType.choices,
        default=SettlementType.CASH,
        verbose_name="结算方式",
        help_text="cash=现结；credit=挂账（需客户开通赊账且不超额度）。",
    )
    payment_method = models.CharField(
        max_length=20,
        choices=PaymentMethod.choices,
        default=PaymentMethod.BANK_TRANSFER,
        verbose_name="支付方式",
    )
    payment_status = models.CharField(
        max_length=24,
        choices=PaymentStatus.choices,
        default=PaymentStatus.UNPAID,
        verbose_name="支付状态",
    )
    stripe_session_id = models.CharField(max_length=255, blank=True, default="", verbose_name="Stripe Session ID")
    paid_at = models.DateTimeField(null=True, blank=True, verbose_name="支付时间")
    transfer_reference = models.CharField(max_length=255, blank=True, default="", verbose_name="转账参考号")
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
    is_debt_counted = models.BooleanField(
        default=False,
        verbose_name="欠款已计入",
        help_text="挂账订单进入已确认后计入欠款；用于防止重复累计。",
    )
    total_revenue = models.FloatField(default=0, verbose_name="总收入")
    total_cost = models.FloatField(default=0, verbose_name="总成本")
    profit = models.FloatField(default=0, verbose_name="利润")

    def __str__(self):
        return self.name

    @property
    def total_amount(self):
        """订单总金额语义字段（兼容前后台），映射到现有 total_revenue。"""
        return float(self.total_revenue or 0.0)


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
    tr = money_dec(0)
    tc = money_dec(0)
    for item in order.items.select_related("product").all():
        tr += money_dec(item.total_revenue)
        tc += money_dec(item.total_cost)
    profit = money_q2(tr - tc)
    Order.objects.filter(pk=order_id).update(
        total_revenue=money_float(tr),
        total_cost=money_float(tc),
        profit=money_float(profit),
    )


def assert_order_fits_available_stock(order_id):
    """未扣库存订单：按商品汇总需求量，校验不超过 current_stock（stock_enabled=True 时）。"""
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
        if not bool(getattr(p, "stock_enabled", True)):
            continue
        cur = float(getattr(p, "current_stock", 0.0))
        if cur < need:
            raise ValidationError(f"库存不足：{p.sku}")


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


def _write_inventory_log(product, qty, before_stock, after_stock, *, add_back=False, order=None, remark=""):
    ctype = InventoryLog.ChangeType.IN if add_back else InventoryLog.ChangeType.OUT
    base_note = remark or ("订单回库" if add_back else "订单扣减")
    if order is not None:
        base_note = f"{base_note}（订单#{order.id}）"
    InventoryLog.objects.create(
        product=product,
        order=order,
        change_type=ctype,
        quantity=float(qty),
        before_stock=float(before_stock),
        after_stock=float(after_stock),
        note=base_note[:240],
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
        # 稳版统一库存：按 Product.current_stock 扣减/回补。
        # 浮点库存可能出现极小精度误差；这里做防负库存的兜底处理。
        eps = 1e-9
        p2 = Product.objects.select_for_update().get(pk=product.pk)
        if not bool(getattr(p2, "stock_enabled", True)):
            # 库存不限：不拦截、不扣减
            return
        cur = float(p2.current_stock or 0.0)
        if add_back:
            new_stock = cur + need
        else:
            if bool(p2.stock_enabled) and cur + eps < need:
                raise ValidationError(f"库存不足：商品「{product.name}」现有 {cur:g}，本次需 {need:g}")
            new_stock = cur - need
        p2.current_stock = max(0.0, float(new_stock))
        p2.save(update_fields=["current_stock"])
        _write_inventory_log(
            p2,
            qty=need,
            before_stock=cur,
            after_stock=float(p2.current_stock),
            add_back=add_back,
            order=order,
            remark=("订单回库" if add_back else "订单扣减"),
        )
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
    quantity = models.FloatField(default=1, verbose_name="数量")
    sale_type = models.CharField(
        max_length=10,
        choices=SaleType.choices,
        default=SaleType.SINGLE,
        verbose_name="销售方式",
    )
    unit_price = models.FloatField(default=0, verbose_name="成交单价")
    unit_cost = models.FloatField(default=0, verbose_name="成交单位成本")
    total_revenue = models.FloatField(default=0, verbose_name="行收入")
    total_cost = models.FloatField(default=0, verbose_name="行成本")
    profit = models.FloatField(default=0, verbose_name="行利润")
    pricing_note = models.CharField(max_length=64, blank=True, default="", verbose_name="折扣来源")

    def __str__(self):
        return f"{self.order.name} - {self.product.name}"

    @property
    def line_total(self):
        """行小计语义字段（兼容前后台），映射到现有 total_revenue。"""
        return float(self.total_revenue or 0.0)

    @property
    def line_cost(self):
        return float(self.total_cost or 0.0)

    @property
    def line_profit(self):
        return float(self.profit or 0.0)

    def _apply_line_amounts(self, p, customer, *, old_item=None):
        """新建行按当前规则计价；已存在行在商品/销售方式不变时冻结单价与折扣说明（仅数量变则重算行金额）。"""
        frozen = (
            old_item is not None
            and old_item.product_id == self.product_id
            and str(old_item.sale_type) == str(self.sale_type)
        )
        if frozen:
            self.unit_price = money_float(old_item.unit_price)
            self.pricing_note = (old_item.pricing_note or "")[:64]
            q = money_dec(self.quantity)
            up = money_dec(self.unit_price)
            tr = money_q2(q * up)
            if self.sale_type == self.SaleType.CASE:
                cpu = money_dec(p.cost_price_case)
            else:
                cpu = money_dec(p.cost_price_single)
            tc = money_q2(q * cpu)
            self.unit_cost = money_float(cpu)
            self.total_revenue = money_float(tr)
            self.total_cost = money_float(tc)
            self.profit = money_float(money_q2(tr - tc))
            return

        unit_price, pricing_note = resolve_selling_unit_price(customer, p, self.sale_type)
        self.unit_price = money_float(unit_price)
        self.pricing_note = pricing_note
        q = money_dec(self.quantity)
        up = money_dec(self.unit_price)
        tr = money_q2(q * up)
        if self.sale_type == self.SaleType.CASE:
            cpu = money_dec(p.cost_price_case)
        else:
            cpu = money_dec(p.cost_price_single)
        tc = money_q2(q * cpu)
        self.unit_cost = money_float(cpu)
        self.total_revenue = money_float(tr)
        self.total_cost = money_float(tc)
        self.profit = money_float(money_q2(tr - tc))

    def save(self, *args, **kwargs):
        if self.quantity <= 0:
            raise ValidationError("数量必须大于 0")

        creating = self.pk is None

        with transaction.atomic():
            order = Order.objects.select_related("customer").get(pk=self.order_id)
            customer = order.customer if order.customer_id else None

            old_item = None
            if not creating:
                old_item = (
                    OrderItem.objects.select_for_update()
                    .select_related("product")
                    .get(pk=self.pk)
                )

            p = Product.objects.select_for_update().get(pk=self.product_id)
            if not p.is_active:
                raise ValidationError("该商品已停用，无法下单")
            if self.sale_type == self.SaleType.SINGLE and not p.can_split_sale:
                raise ValidationError("该商品不可拆卖，请选择整箱")
            if float(self.quantity) < float(p.minimum_order_qty):
                raise ValidationError(f"数量不能低于起订量 {p.minimum_order_qty}")

            self._apply_line_amounts(p, customer, old_item=old_item)

            if order.stock_deducted:
                if creating:
                    need = _stock_need_for_line(self, p)
                    _apply_need_to_inventory(p, need, add_back=False, order=order)
                    super().save(*args, **kwargs)
                    recalculate_order_totals(self.order_id)
                    return

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


class CreditApplication(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "待审核"
        APPROVED = "approved", "已通过"
        REJECTED = "rejected", "已拒绝"

    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="credit_applications", verbose_name="客户")
    shop_name = models.CharField(max_length=200, blank=True, default="", verbose_name="店名")
    contact_name = models.CharField(max_length=100, blank=True, default="", verbose_name="联系人")
    phone = models.CharField(max_length=30, blank=True, default="", verbose_name="手机号")
    monthly_purchase_estimate = models.FloatField(default=0, verbose_name="月采购额预估")
    requested_credit_limit = models.FloatField(default=0, verbose_name="申请额度")
    note = models.TextField(blank=True, default="", verbose_name="备注")
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING, verbose_name="审核状态")
    approved_credit_limit = models.FloatField(default=0, verbose_name="审批额度")
    review_note = models.TextField(blank=True, default="", verbose_name="审批备注")
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="申请时间")
    reviewed_at = models.DateTimeField(null=True, blank=True, verbose_name="审核时间")

    class Meta:
        ordering = ("-created_at",)
        verbose_name = "信用额度申请"
        verbose_name_plural = "信用额度申请"

    def __str__(self):
        return f"{self.customer.name} 额度申请 {self.get_status_display()}"

    def save(self, *args, **kwargs):
        is_reviewed = self.status in (self.Status.APPROVED, self.Status.REJECTED)
        if is_reviewed and self.reviewed_at is None:
            self.reviewed_at = timezone.now()
        if not is_reviewed:
            self.reviewed_at = None
        super().save(*args, **kwargs)
        if self.status == self.Status.APPROVED:
            approved_limit = float(self.approved_credit_limit or 0)
            if approved_limit <= 0:
                approved_limit = float(self.requested_credit_limit or 0)
            Customer.objects.filter(pk=self.customer_id).update(
                allow_credit=True,
                credit_limit=max(0.0, approved_limit),
            )
            cust = Customer.objects.get(pk=self.customer_id)
            cust.save()


def _discount_rates_for_level(level_key):
    """返回 (单品率, 整箱率)；与 CUSTOMER_LEVEL_DISCOUNT_RATES 一致；未知等级按 1.0。"""
    r = float(CUSTOMER_LEVEL_DISCOUNT_RATES.get(level_key, 1.0))
    return r, r


def _format_discount_source(level_key, rate, kind_label):
    """kind_label: 单品 / 整箱"""
    if abs(rate - 1.0) < 1e-9:
        return f"{level_key}级原价（{kind_label}）"
    zhe = int(round(rate * 100))
    return f"{level_key}级{zhe}折（{kind_label}）"


def resolve_product_price_for_customer(product, customer, sale_type):
    """
    统一价格解析（商城/录单/订单明细/校验共用）。sale_type: OrderItem.SaleType 或 'single' / 'case'。

    优先级（固定）：
    1) 启用中的客户商品专属价（>0）
    2) 客户等级折扣（CUSTOMER_LEVEL_DISCOUNT_RATES）
    3) 商品原价

    返回 dict: original_price, final_price, discount_source（写入明细 pricing_note）
    """
    is_case = str(sale_type) == "case"
    original = money_float(product.price_case if is_case else product.price_single)
    kind_label = "整箱" if is_case else "单品"

    if customer is None:
        return {
            "original_price": original,
            "final_price": original,
            "discount_source": "基础价",
        }

    cp = (
        CustomerProductPrice.objects.filter(customer=customer, product=product, is_active=True)
        .only("custom_price_single", "custom_price_case")
        .first()
    )
    if cp:
        ex = money_float(cp.custom_price_case if is_case else cp.custom_price_single)
        if ex > 0:
            return {
                "original_price": original,
                "final_price": ex,
                "discount_source": "专属价",
            }

    tier = customer.customer_level
    sr, cr = _discount_rates_for_level(tier)
    rate = cr if is_case else sr
    final = money_float(money_q2(money_dec(original) * money_dec(rate)))
    note = _format_discount_source(tier, rate, kind_label)
    return {
        "original_price": original,
        "final_price": final,
        "discount_source": note,
    }


def resolve_selling_unit_price(customer, product, sale_type):
    """兼容旧接口：(成交价, 定价说明)。"""
    res = resolve_product_price_for_customer(product, customer, sale_type)
    note = res["discount_source"]
    if len(note) > 64:
        note = note[:64]
    return money_float(res["final_price"]), note


def total_spent_for_customer(customer):
    total = money_dec(0)
    for item in OrderItem.objects.filter(order__customer=customer).select_related("product"):
        total += money_dec(item.total_revenue)
    return money_float(total)


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
    # 若老板已人工审批开通赊账（allow_credit=True 且额度>0），不被消费等级规则覆盖。
    if bool(customer.allow_credit) and float(customer.credit_limit or 0.0) > 0:
        customer.save(update_fields=["customer_level"])
        return
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
    if instance.price_on_request is None:
        instance.price_on_request = False
    if instance.stock_quantity is None:
        instance.stock_quantity = 0.0
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
