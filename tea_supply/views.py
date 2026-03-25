import csv
import io
import json
import hashlib
import logging
from decimal import Decimal, ROUND_HALF_UP
import secrets
import time
import os
from functools import wraps
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, F, Min, Q, Sum
from django.db.models.functions import Coalesce, TruncDate
from django.http import HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from .credit_debt import apply_credit_debt_if_needed, reverse_credit_debt_if_counted
from .money_utils import money_dec, money_float, money_q2
from tea_supply.utils.pricing import resolve_product_price_for_customer
from .models import (
    CUSTOMER_LEVEL_DISCOUNT_RATES,
    CreditApplication,
    Customer,
    CustomerProductPrice,
    Company,
    Ingredient,
    Order,
    OrderItem,
    Product,
    ProductCategory,
    UserRole,
    calculate_reorder,
    deduct_stock_for_order,
    recalculate_order_totals,
    update_customer_level,
    _stock_need_for_line,
    resolve_selling_unit_price,
)
from .models import UserCompanyProfile

logger = logging.getLogger(__name__)
PROFIT_PROTECTION_MODE = "warning"  # "warning" | "block"


def _profit_recommendation_rows(*, company=None, limit=5):
    base_qs = OrderItem.objects.exclude(
        order__workflow_status=Order.WorkflowStatus.CANCELLED
    )
    if company is not None:
        base_qs = base_qs.filter(order__company=company)
    base = (
        base_qs
        .values("product_id", "product__name", "product__sku")
        .annotate(
            qty=Coalesce(Sum("quantity"), 0.0),
            total_profit=Coalesce(Sum("profit"), 0.0),
        )
    )
    top_profit_ids = set(
        r["product_id"] for r in base.order_by("-total_profit")[: max(limit, 5)]
    )
    top_unit_ids = set(
        r["product_id"]
        for r in base.annotate(
            unit_profit=Coalesce(Sum("profit"), 0.0)
            / (Coalesce(Sum("quantity"), 0.0) + 1e-9)
        )
        .order_by("-unit_profit")[: max(limit, 5)]
    )
    top_qty_ids = set(
        r["product_id"] for r in base.order_by("-qty")[: max(limit, 5)]
    )

    rows = []
    for r in base.order_by("-total_profit")[:limit]:
        pid = r["product_id"]
        qty = float(r["qty"] or 0.0)
        total_profit = float(r["total_profit"] or 0.0)
        unit_profit = 0.0 if qty <= 0 else money_float(total_profit / qty)

        if pid in top_unit_ids:
            reason = "高利润商品"
        elif pid in top_qty_ids:
            reason = "畅销商品"
        elif pid in top_profit_ids:
            reason = "高收益商品"
        else:
            reason = "推荐商品"

        rows.append(
            {
                "product_id": pid,
                "sku": r["product__sku"],
                "name": r["product__name"],
                "qty": qty,
                "total_profit": money_float(total_profit),
                "unit_profit": unit_profit,
                "reason": reason,
            }
        )
    return rows


def tier_discount_map_for_wholesale():
    """录单页 JS：等级 -> {single, case} 折扣率（与 CUSTOMER_LEVEL_DISCOUNT_RATES 一致）。"""
    out = {}
    for code, _ in Customer.ValueLevel.choices:
        r = float(CUSTOMER_LEVEL_DISCOUNT_RATES.get(code, 1.0))
        out[code] = {"single": r, "case": r}
    return out


def tier_rules_banner_text():
    """Tier discount line for shop header (aligned with CUSTOMER_LEVEL_DISCOUNT_RATES)."""
    parts = []
    for lvl in ["NORMAL", "VIP", "PREMIUM"]:
        r = float(CUSTOMER_LEVEL_DISCOUNT_RATES.get(lvl, 1.0))
        if abs(r - 1.0) < 1e-9:
            parts.append(f"{lvl} base price")
        else:
            pct_off = int(round((1.0 - r) * 100))
            parts.append(f"{lvl} {pct_off}% off")
    return " · ".join(parts)


def _unsettled_order_statuses():
    return (Order.Status.PENDING,)


def _is_internal_user(user):
    if not user.is_authenticated:
        return False
    rp = getattr(user, "role_profile", None)
    if rp and rp.role in (UserRole.Role.OWNER, UserRole.Role.STAFF):
        return True
    return bool(user.is_staff)


def _is_boss(user):
    if not user.is_authenticated:
        return False
    rp = getattr(user, "role_profile", None)
    if rp and rp.role == UserRole.Role.OWNER:
        return True
    return bool(user.is_superuser)


def get_user_company(user):
    if not user or not getattr(user, "is_authenticated", False):
        return None
    prof = getattr(user, "company_profile", None)
    if prof and getattr(prof, "company_id", None):
        return prof.company
    return None


def internal_user_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_internal_user(request.user):
            return HttpResponseForbidden("You do not have access to this page.")
        return view_func(request, *args, **kwargs)

    return _wrapped


def boss_required(view_func):
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        if not _is_boss(request.user):
            return HttpResponseForbidden(
                "Only the business owner may access this page."
            )
        return view_func(request, *args, **kwargs)

    return _wrapped


def unsettled_amount_for_customer(customer):
    """仅统计待处理（pending）订单明细；已结算（paid）不计入应收账款 / 额度占用。"""
    total = 0.0
    for item in OrderItem.objects.filter(
        order__customer=customer,
        order__status__in=_unsettled_order_statuses(),
    ).select_related("product"):
        total += float(item.total_revenue)
    return total


def _days_since_earliest_pending(earliest_created_at):
    if not earliest_created_at:
        return 0
    dt = earliest_created_at
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    today = timezone.now().date()
    return max(0, (today - dt.date()).days)


def _make_order_submit_signature(
    *, prefix: str, customer_id: str, lines_json: str, extra=None
):
    """
    用于服务端防重复提交（不改变业务流程）：签名同一请求体在很短时间内重复则拒绝。
    """
    payload = {
        "prefix": prefix,
        "customer_id": customer_id,
        "lines_json": lines_json,
        "extra": extra or {},
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _check_and_set_submit_lock(
    *, request, lock_key: str, signature: str, window_seconds: int = 5
) -> bool:
    """
    返回 True 表示检测到重复提交；False 表示写入锁并允许继续。
    """
    now = time.time()
    last = request.session.get(lock_key) or {}
    last_sig = last.get("sig")
    last_ts = float(last.get("ts") or 0.0)
    if last_sig == signature and (now - last_ts) <= window_seconds:
        return True
    request.session[lock_key] = {"sig": signature, "ts": now}
    request.session.modified = True
    return False


def _clear_submit_lock_if_matches(*, request, lock_key: str, signature: str):
    last = request.session.get(lock_key) or {}
    if last.get("sig") == signature:
        request.session.pop(lock_key, None)
        request.session.modified = True


def _ensure_request_session_key(request):
    """商城下单：保证 session 存在并返回 session_key（无 request 时返回空）。"""
    if request is None:
        return ""
    if not request.session.session_key:
        request.session.create()
    sk = (request.session.session_key or "").strip()
    if not sk:
        request.session.save()
        sk = (request.session.session_key or "").strip()
    return sk[:40]


def _guest_order_session_key(request, explicit=None):
    if explicit and str(explicit).strip():
        return str(explicit).strip()[:40]
    sk = _ensure_request_session_key(request)
    if sk:
        return sk
    return secrets.token_hex(20)[:40]


def submit_order_from_lines(
    request,
    customer_obj,
    lines,
    *,
    from_shop=False,
    shipping=None,
    guest_session_key=None,
):
    """
    从购物车明细创建订单（批发录单 / 客户商城共用逻辑）。
    request: 可为 None（店员录单）；商城下单传入 request 以绑定 session。
    lines: 已解析的 list，元素为 dict：product_id, sale_type, quantity
    from_shop=True 时仍校验账号/停单等；仅跳过挂账额度数值校验（现结由老板线下对账）。
    """
    try:
        if not isinstance(lines, list) or len(lines) == 0:
            raise ValidationError("Add at least one line item before submitting.")

        shipping = shipping or {}
        settlement_type = (
            str(shipping.get("settlement_type") or Order.SettlementType.CASH)
            .strip()
            .lower()
        )
        if settlement_type not in (
            Order.SettlementType.CASH,
            Order.SettlementType.CREDIT,
        ):
            settlement_type = Order.SettlementType.CASH
        payment_method = (
            str(shipping.get("payment_method") or Order.PaymentMethod.BANK_TRANSFER)
            .strip()
            .lower()
        )
        valid_payment_methods = {
            Order.PaymentMethod.BANK_TRANSFER,
            Order.PaymentMethod.CHECK,
            Order.PaymentMethod.CARD_ON_PICKUP,
            Order.PaymentMethod.CASH,
            Order.PaymentMethod.CREDIT,
        }
        if payment_method not in valid_payment_methods:
            payment_method = Order.PaymentMethod.BANK_TRANSFER
        if payment_method == Order.PaymentMethod.CREDIT:
            settlement_type = Order.SettlementType.CREDIT
        else:
            settlement_type = Order.SettlementType.CASH
        if from_shop:
            cn = (shipping.get("contact_name") or "").strip()
            phone = (shipping.get("delivery_phone") or "").strip()
            addr = (shipping.get("delivery_address") or "").strip()
            if not cn:
                raise ValidationError("Contact name is required.")
            if not phone:
                raise ValidationError("Phone number is required.")
            if not addr:
                raise ValidationError("Delivery address is required.")
            if customer_obj is not None:
                reason = customer_obj.shop_order_denial_reason()
                if reason:
                    raise ValidationError(reason)
        else:
            if customer_obj is None:
                raise ValidationError("Please select a customer.")

        if customer_obj is not None and customer_obj.is_blocked:
            raise ValidationError(Customer.ORDER_STOP_SUPPLIER_MESSAGE)

        current_unsettled = (
            money_float(customer_obj.current_debt) if customer_obj else 0.0
        )
        credit_limit = money_float(customer_obj.credit_limit) if customer_obj else 0.0

        order_total = money_dec(0)
        validated = []
        profit_risk_warnings = []
        for raw in lines:
            if raw.get("product_id") is None:
                raise ValidationError("A line item is missing a product.")
            pid = int(raw["product_id"])
            sale_type = raw.get("sale_type", OrderItem.SaleType.SINGLE)
            qraw = raw.get("quantity", 1)
            try:
                qty = float(qraw)
            except (TypeError, ValueError):
                qty = 1.0
            if qty <= 0:
                qty = 1.0
            if sale_type not in (OrderItem.SaleType.SINGLE, OrderItem.SaleType.CASE):
                raise ValidationError("Invalid sale type.")

            p = Product.objects.get(pk=pid)
            logger.info(
                "CHECKOUT_CART_LINE sku=%s product_id=%s qty=%s sale_type=%s",
                p.sku,
                pid,
                qty,
                sale_type,
            )
            if not p.is_active:
                raise ValidationError(f'Product "{p.name}" is inactive.')
            if sale_type == OrderItem.SaleType.SINGLE and not p.can_split_sale:
                raise ValidationError(
                    f'"{p.name}" cannot be sold in single units. Choose case.'
                )
            if qty < float(p.minimum_order_qty):
                raise ValidationError(
                    'Quantity for "%s" cannot be below MOQ %s'
                    % (p.name, p.minimum_order_qty)
                )

            price_info = resolve_product_price_for_customer(
                product=p,
                customer=customer_obj,
                sale_type=sale_type,
            )
            assert price_info["source"] in ["custom", "tier", "base"]
            final_unit_price = money_dec(price_info["final_price"])
            if money_float(final_unit_price) <= 0:
                raise ValidationError(
                    f'No valid price for "{p.name}". Request a quote.'
                )
            final_subtotal = money_q2(money_dec(qty) * final_unit_price)
            final_pricing_note = str(price_info.get("pricing_note") or "")
            source = str(price_info.get("source") or "")
            if source == "custom":
                final_pricing_note = "Customer exclusive price"
            auto_cost_updated = False
            if not p.cost_price_single or float(p.cost_price_single) == 0:
                p.cost_price_single = float(
                    (final_unit_price * Decimal("0.6")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                auto_cost_updated = True
            if not p.cost_price_case or float(p.cost_price_case) == 0:
                p.cost_price_case = float(
                    (final_unit_price * Decimal("0.6")).quantize(
                        Decimal("0.01"), rounding=ROUND_HALF_UP
                    )
                )
                auto_cost_updated = True
            if auto_cost_updated:
                p.save(update_fields=["cost_price_single", "cost_price_case"])
                print(
                    "AUTO_COST_FILLED",
                    {
                        "product": p.id,
                        "sku": p.sku,
                        "cost_single": str(p.cost_price_single),
                        "cost_case": str(p.cost_price_case),
                    },
                )
            print(
                "PROFIT_DEBUG",
                {
                    "product": p.name,
                    "sale_type": sale_type,
                    "unit_price": str(final_unit_price),
                    "cost_single": str(getattr(p, "cost_price_single", None)),
                    "cost_case": str(getattr(p, "cost_price_case", None)),
                },
            )
            cost_unit_price = money_dec(
                p.cost_price_case
                if sale_type == OrderItem.SaleType.CASE
                else p.cost_price_single
            )
            if sale_type == OrderItem.SaleType.SINGLE and cost_unit_price <= money_dec(
                0
            ):
                raise ValidationError("Single-unit cost is not set for this product.")
            if sale_type == OrderItem.SaleType.CASE and cost_unit_price <= money_dec(0):
                raise ValidationError("Case cost is not set for this product.")
            unit_profit = money_q2(final_unit_price - cost_unit_price)
            if unit_profit < money_dec(0):
                risk_msg = (
                    f"Price below cost for {p.sku} "
                    f"(sale {money_float(final_unit_price):.2f} < cost {money_float(cost_unit_price):.2f})"
                )
                if PROFIT_PROTECTION_MODE == "block":
                    raise ValidationError("Sale price is below cost; order blocked.")
                profit_risk_warnings.append(risk_msg)
            order_total += final_subtotal
            validated.append(
                {
                    "product_id": pid,
                    "sale_type": sale_type,
                    "quantity": qty,
                    "final_unit_price": final_unit_price,
                    "final_subtotal": final_subtotal,
                    "final_pricing_note": final_pricing_note,
                    "source": source,
                    "cost_unit_price": cost_unit_price,
                    "unit_profit": unit_profit,
                }
            )

        needs = defaultdict(float)
        for v in validated:
            p = Product.objects.get(pk=v["product_id"])
            probe = OrderItem(
                product=p, quantity=v["quantity"], sale_type=v["sale_type"]
            )
            needs[v["product_id"]] += float(_stock_need_for_line(probe, p))
        for pid, need in needs.items():
            if need <= 0:
                continue
            p = Product.objects.get(pk=pid)
            if not bool(getattr(p, "stock_enabled", True)):
                continue
            cur = float(getattr(p, "stock", 0.0))
            logger.info(
                "CHECKOUT_STOCK_VALIDATE sku=%s product_id=%s required_qty=%s stock=%s",
                p.sku,
                pid,
                float(need),
                cur,
            )
            if cur < need:
                raise ValidationError(f"Insufficient stock: {p.sku}")

        if settlement_type == Order.SettlementType.CREDIT:
            if customer_obj is None:
                raise ValidationError("Credit orders must have a customer.")
            if not customer_obj.allow_credit:
                raise ValidationError("Credit is not allowed for this order.")
            if credit_limit <= 0:
                raise ValidationError("Over credit limit; order cannot be placed.")
            if current_unsettled >= credit_limit:
                raise ValidationError("Over credit limit; order cannot be placed.")
            if money_dec(current_unsettled) + order_total > money_dec(credit_limit):
                raise ValidationError("Over credit limit; order cannot be placed.")

        with transaction.atomic():
            customer_locked = None
            if customer_obj is not None:
                customer_locked = Customer.objects.select_for_update().get(
                    pk=customer_obj.pk
                )
            company = getattr(customer_locked, "company", None) if customer_locked else None

            # 1) 先组装订单头字段（每次提交只创建一条 Order）
            create_kwargs = {"confirmed": False}
            if company is not None:
                create_kwargs["company"] = company
            if customer_locked is not None:
                create_kwargs["customer"] = customer_locked
            if (
                request is not None
                and getattr(request, "user", None)
                and request.user.is_authenticated
            ):
                create_kwargs["ordered_by"] = request.user
            elif customer_locked is not None and customer_locked.user_id:
                create_kwargs["ordered_by_id"] = customer_locked.user_id
            if from_shop:
                ts = timezone.now().strftime("%m%d%H%M")
                if customer_locked is not None:
                    create_kwargs["name"] = f"Shop-{customer_locked.name}-{ts}"
                    create_kwargs["guest_session_key"] = ""
                else:
                    create_kwargs["name"] = f"Shop-Guest-{ts}"
                    create_kwargs["guest_session_key"] = _guest_order_session_key(
                        request, guest_session_key
                    )
                create_kwargs["workflow_status"] = Order.WorkflowStatus.PENDING_CONFIRM
                create_kwargs["settlement_type"] = settlement_type
                create_kwargs["payment_method"] = payment_method
                create_kwargs["payment_status"] = (
                    Order.PaymentStatus.PENDING_CONFIRMATION
                )
                create_kwargs["contact_name"] = (shipping.get("contact_name") or "")[
                    :100
                ]
                create_kwargs["delivery_phone"] = (
                    shipping.get("delivery_phone") or ""
                )[:30]
                create_kwargs["store_name"] = (shipping.get("store_name") or "")[:200]
                create_kwargs["delivery_address"] = (
                    shipping.get("delivery_address") or ""
                )[:500]
                note = (shipping.get("order_note") or "")[:2000]
                check_no = (shipping.get("check_number") or "").strip()
                if payment_method == Order.PaymentMethod.CARD_ON_PICKUP:
                    note = (note + "\nPay at warehouse / pickup").strip()
                elif payment_method == Order.PaymentMethod.CASH:
                    note = (note + "\nCash payment").strip()
                elif payment_method == Order.PaymentMethod.CREDIT:
                    note = (
                        note + "\nOn account — pending finance confirmation"
                    ).strip()
                elif payment_method == Order.PaymentMethod.CHECK and check_no:
                    note = (note + f"\nCheck no.: {check_no[:100]}").strip()
                create_kwargs["order_note"] = note[:2000]
                create_kwargs["transfer_reference"] = (
                    shipping.get("transfer_reference") or ""
                )[:255]
            else:
                create_kwargs["customer"] = customer_locked
                create_kwargs["settlement_type"] = settlement_type
                create_kwargs["payment_method"] = payment_method

            # 2) 创建订单头（一次提交仅一条）
            order = Order.objects.create(**create_kwargs)
            if profit_risk_warnings:
                warning_text = "\n".join(profit_risk_warnings)
                order.order_note = (
                    (order.order_note or "").strip() + "\n" + warning_text
                ).strip()[:2000]
                order.save(update_fields=["order_note"])

            # 3) 明细只写 OrderItem（不重复建 Order）
            for v in validated:
                product = Product.objects.get(pk=v["product_id"])
                sale_type = v["sale_type"]
                qty = v["quantity"]
                source = v["source"]
                final_unit_price = v["final_unit_price"]
                final_subtotal = v["final_subtotal"]
                final_pricing_note = v["final_pricing_note"]
                print(
                    "PRICE_SAVE_DEBUG",
                    {
                        "product_id": product.id,
                        "product_sku": getattr(product, "sku", ""),
                        "customer_id": getattr(customer_locked, "id", None),
                        "sale_type": sale_type,
                        "source": source,
                        "unit_price": str(final_unit_price),
                        "subtotal": str(final_subtotal),
                        "pricing_note": final_pricing_note,
                    },
                )
                OrderItem.objects.create(
                    order=order,
                    product=product,
                    sale_type=sale_type,
                    quantity=qty,
                    unit_price=money_float(final_unit_price),
                    total_revenue=money_float(final_subtotal),
                    pricing_note=(final_pricing_note or "")[:64],
                )

            # 4) 统一重算总金额：order.total_amount = sum(order_items.line_total)
            recalculate_order_totals(order.id)

            # 5) 基础库存联动：下单成功即扣减；不足时抛错并整体回滚
            deduct_stock_for_order(order.id)
            # 6) 挂账：重算总额后计入客户欠款（幂等）；非挂账不处理
            apply_credit_debt_if_needed(order)
        if profit_risk_warnings and request is not None:
            first_msg = profit_risk_warnings[0]
            messages.warning(request, f"⚠️ {first_msg}")
        return order
    except Exception as e:
        print(e)
        raise


def ensure_customer_profile(user):
    """
    为已登录商城用户补建 Customer（不覆盖已有记录）。
    staff/superuser 不自动创建，避免污染内部账号；若已手动绑定则返回该档案。
    """
    if not user.is_authenticated:
        return None
    company = get_user_company(user)
    existing = Customer.objects.filter(user_id=user.id, company=company).first()
    if existing:
        return existing
    if user.is_staff or user.is_superuser:
        return None
    username = (user.username or "").strip() or f"user{user.pk}"
    defaults = {
        "name": username[:100],
        "company": company,
        "contact_name": username[:100],
        "phone": username[:30],
        "shop_name": username[:200],
        "address": "To be completed",
        "delivery_zone": "Unassigned",
        "customer_level": Customer.Level.C,
        "allow_credit": False,
        "credit_limit": 0.0,
        "current_debt": 0.0,
        "minimum_order_amount": 0.0,
        "payment_cycle": Customer.PaymentCycle.CASH,
        "account_status": Customer.AccountStatus.APPROVED,
        "is_active": True,
    }
    try:
        with transaction.atomic():
            customer, _created = Customer.objects.get_or_create(
                user=user, company=company, defaults=defaults
            )
        return customer
    except IntegrityError:
        return Customer.objects.filter(user_id=user.id, company=company).first()


def get_shop_customer(request):
    u = getattr(request, "user", None)
    if not u or not u.is_authenticated:
        return None
    return ensure_customer_profile(u)


def _ensure_customer_role(user):
    UserRole.objects.update_or_create(
        user=user, defaults={"role": UserRole.Role.CUSTOMER}
    )


def shop_order_permission(customer):
    """
    商城是否允许提交订单。
    返回 (can_order: bool, block_hint: str)；block_hint 在不可下单时用于按钮提示/弹窗。
    仅已登录且绑定客户档案的用户可下单。
    """
    if not customer:
        return (
            False,
            "Please sign in to place an order (register if you have no account).",
        )
    reason = customer.shop_order_denial_reason()
    if reason:
        return False, reason
    return True, ""


def _default_product_image_url():
    """无商品图时使用 static/images/default.png（见 settings.DEFAULT_PRODUCT_IMAGE_STATIC）。"""
    su = settings.STATIC_URL
    su = str(su)
    if not su.startswith("/"):
        su = "/" + su.lstrip("/")
    rel = getattr(settings, "DEFAULT_PRODUCT_IMAGE_STATIC", "images/default.png")
    return su.rstrip("/") + "/" + str(rel).lstrip("/")


def _shop_product_row(customer, p):
    """客户商城商品 JSON 行（列表页 / 详情页共用）。"""
    # 图片规则：
    # 1) 后台上传图（catalog_upload）优先
    # 2) 其次使用 image 字段（相对 media 路径）
    # 3) 若 image 为空，自动回退 /media/products/{sku}.jpg
    image_url = ""
    image_path = (getattr(p, "image", None) or "").strip()
    has_image = False
    try:
        cu = getattr(p, "catalog_upload", None)
        if cu:
            image_url = cu.url
            has_image = True
    except Exception:
        pass
    if not image_url:
        if image_path:
            image_url = "/media/" + image_path.lstrip("/")
            has_image = True
        else:
            image_url = f"/media/products/{p.sku}.jpg"
    base_s = money_float(p.price_single)
    base_c = money_float(p.price_case)
    ds, note_s = resolve_selling_unit_price(customer, p, OrderItem.SaleType.SINGLE)
    dc, note_c = resolve_selling_unit_price(customer, p, OrderItem.SaleType.CASE)
    stock_disp = float(getattr(p, "stock", 0.0))
    safety = float(getattr(p, "safety_stock", 10.0))
    enabled = bool(getattr(p, "stock_enabled", True))
    excl = "Customer exclusive price"
    if excl in (note_s, note_c) or "客户专属价" in (note_s, note_c):
        price_note = excl
    else:
        price_note = (
            f"Per bag: {note_s} · Per case: {note_c}" if note_s != note_c else note_s
        )
    if customer is None:
        price_note = "Base price"
    row = {
        "id": p.id,
        "category_id": int(p.category_id) if p.category_id else None,
        "category_name": p.category.name if p.category_id else "",
        "name": p.name,
        "sku": p.sku,
        "unit_label": (p.unit_label or "").strip() or "per unit",
        "case_label": (p.case_label or "").strip() or "per case",
        "image": image_path,
        "price_single": base_s,
        "price_case": base_c,
        "base_single": base_s,
        "base_case": base_c,
        "display_single": money_float(ds),
        "display_case": money_float(dc),
        "strike_single": abs(money_float(ds) - base_s) > 1e-6,
        "strike_case": abs(money_float(dc) - base_c) > 1e-6,
        "price_note": price_note,
        "price_note_single": note_s,
        "price_note_case": note_c,
        "can_split_sale": p.can_split_sale,
        "minimum_order_qty": float(p.minimum_order_qty),
        "image_url": image_url,
        "has_image": has_image,
        "price_on_request": base_s <= 0 and base_c <= 0,
        "can_quote_single": base_s > 0,
        "can_quote_case": base_c > 0,
        "stock_quantity": stock_disp,
        "current_stock": stock_disp,
        "safety_stock": safety,
        "stock_enabled": enabled,
        "is_out_of_stock": enabled and stock_disp <= 0,
        "is_low_stock": enabled and stock_disp > 0 and stock_disp <= safety,
        "uses_ingredient_stock": bool(p.ingredient_id),
        "units_per_case": float(p.units_per_case),
        "cost_single": money_float(p.cost_price_single),
        "cost_case": money_float(p.cost_price_case),
        "profit_risk_single": money_float(ds) < money_float(p.cost_price_single),
        "profit_risk_case": money_float(dc) < money_float(p.cost_price_case),
    }
    return row


def _dunning_time_and_supply(amount, credit_limit, days_since_earliest):
    """超额度优先；否则按最早待处理订单未结算天数分档。"""
    amt = float(amount)
    cl = float(credit_limit)
    no_limit = cl <= 0
    if cl > 0 and amt > cl:
        return {
            "dunning_status": "High risk",
            "supply_advice": "Suspend supply",
            "style": "high_risk",
            "no_limit": False,
        }
    if amt <= 0:
        return {
            "dunning_status": "OK",
            "supply_advice": "May continue supply",
            "style": "normal",
            "no_limit": no_limit,
        }
    d = int(days_since_earliest)
    if d < 3:
        return {
            "dunning_status": "OK",
            "supply_advice": "Cash terms" if no_limit else "May continue supply",
            "style": "normal",
            "no_limit": no_limit,
        }
    if d <= 7:
        return {
            "dunning_status": "Reminder",
            "supply_advice": "Reconcile account",
            "style": "watch",
            "no_limit": no_limit,
        }
    if d <= 15:
        return {
            "dunning_status": "Collection",
            "supply_advice": "Prioritize collection",
            "style": "dunning",
            "no_limit": no_limit,
        }
    return {
        "dunning_status": "Urgent collection",
        "supply_advice": "Collect now; consider stopping supply",
        "style": "severe",
        "no_limit": no_limit,
    }


@login_required
@internal_user_required
def wholesale_order_entry(request):
    company = get_user_company(request.user)
    customers = Customer.objects.all().order_by("name")
    categories = ProductCategory.objects.filter(is_active=True).order_by(
        "sort_order", "id"
    )
    products = (
        Product.objects.filter(is_active=True)
        .select_related("category")
        .order_by("category", "name")
    )
    if company is not None:
        customers = customers.filter(company=company)
        products = products.filter(company=company)
    products_data = []
    for p in products:
        products_data.append(
            {
                "id": p.id,
                "category_id": p.category_id,
                "name": p.name,
                "sku": p.sku,
                "unit_label": p.unit_label,
                "case_label": p.case_label,
                "price_single": float(p.price_single),
                "price_case": float(p.price_case),
                "can_split_sale": p.can_split_sale,
                "minimum_order_qty": float(p.minimum_order_qty),
            }
        )

    customer_rows = [
        {"customer": c, "unsettled_amount": unsettled_amount_for_customer(c)}
        for c in customers
    ]

    if request.method == "POST":
        customer_id = request.POST.get("customer")
        lines_raw = request.POST.get("lines_json", "[]")

        try:
            # 防重复提交：避免双击导致生成两张订单。
            sig = _make_order_submit_signature(
                prefix="wholesale",
                customer_id=str(customer_id),
                lines_json=str(lines_raw),
                extra={},
            )
            lock_key = f"submit_lock::wholesale::{customer_id}"
            lock_set = False
            if _check_and_set_submit_lock(
                request=request, lock_key=lock_key, signature=sig
            ):
                messages.error(
                    request,
                    "Duplicate submit detected. Please do not click submit twice.",
                )
                return redirect("wholesale-order-entry")
            lock_set = True

            lines = json.loads(lines_raw)
            customer_obj = Customer.objects.get(pk=customer_id)
            submit_order_from_lines(request, customer_obj, lines)
            _clear_submit_lock_if_matches(
                request=request, lock_key=lock_key, signature=sig
            )
            messages.success(request, "Order saved.")
            return redirect("wholesale-order-entry")
        except Customer.DoesNotExist as exc:
            print(exc)
            messages.error(
                request, "Customer or product not found. Refresh and try again."
            )
        except Product.DoesNotExist as exc:
            print(exc)
            messages.error(
                request, "Customer or product not found. Refresh and try again."
            )
        except (ValueError, TypeError, ValidationError) as exc:
            print(exc)
            messages.error(request, str(exc))
        except json.JSONDecodeError as exc:
            print(exc)
            messages.error(request, "Invalid line item format.")
        finally:
            # 失败也清锁，允许用户修正后重试
            # （只有签名一致才会清除，避免误删其他请求的锁）
            try:
                sig = locals().get("sig")
                lock_key = locals().get("lock_key")
                lock_set = locals().get("lock_set", False)
                if sig and lock_key and lock_set:
                    _clear_submit_lock_if_matches(
                        request=request, lock_key=lock_key, signature=sig
                    )
            except Exception:
                pass

    exclusive_map = {}
    for cp in CustomerProductPrice.objects.filter(is_active=True).only(
        "customer_id", "product_id", "custom_price_single", "custom_price_case"
    ):
        cid = str(cp.customer_id)
        pid = str(cp.product_id)
        exclusive_map.setdefault(cid, {})[pid] = {
            "price_single": float(cp.custom_price_single),
            "price_case": float(cp.custom_price_case),
        }

    context = {
        "customer_rows": customer_rows,
        "categories": categories,
        "products": products,
        "products_data": products_data,
        "products_json": json.dumps(products_data, ensure_ascii=False),
        "tier_discounts_json": json.dumps(
            tier_discount_map_for_wholesale(), ensure_ascii=False
        ),
        "exclusive_prices_json": json.dumps(exclusive_map, ensure_ascii=False),
        "today_order_count": Order.objects.count(),
        "recommended_product_ids": [
            r["product_id"] for r in _profit_recommendation_rows(company=company, limit=5)
        ],
    }
    return render(request, "wholesale_order_form.html", context)


def shop_home(request):
    """客户前台商城（/shop/）：商品与分类均来自数据库；定价随 request.user 客户身份变化。"""
    categories = ProductCategory.objects.filter(is_active=True).order_by(
        "sort_order", "id"
    )
    customer = get_shop_customer(request)
    company = getattr(customer, "company", None) if customer else None
    products = (
        Product.objects.filter(is_active=True)
        .select_related("category", "ingredient")
        .order_by("category__sort_order", "category_id", "name")
    )
    if company is not None:
        products = products.filter(company=company)
    shop_items = []
    missing_images = []
    missing_prices = []
    for p in products:
        row = _shop_product_row(customer, p)
        if row["can_split_sale"] and row["can_quote_single"]:
            row["default_mode"] = "single"
        elif row["can_quote_case"]:
            row["default_mode"] = "case"
        elif not row["can_split_sale"]:
            row["default_mode"] = "case"
        else:
            row["default_mode"] = "single"
        shop_items.append(row)
        if row["price_on_request"]:
            missing_prices.append({"sku": p.sku, "name": p.name})

    can_order, order_hint = shop_order_permission(customer)
    ctx = {
        "categories": categories,
        "products": shop_items,
        "shop_products": shop_items,
        "shop_customer": customer,
        "shop_unsettled": unsettled_amount_for_customer(customer) if customer else None,
        "shop_can_order": can_order,
        "shop_order_block_hint": order_hint,
        "shop_logged_in": bool(
            getattr(request, "user", None) and request.user.is_authenticated
        ),
        "missing_images": missing_images,
        "missing_prices": missing_prices,
        "tier_rules_banner": tier_rules_banner_text(),
    }
    return render(request, "shop/shop_home.html", ctx)


def register_view(request):
    """客户自助注册（最小实现）：创建 User + 绑定 Customer，登录后跳转商城。"""
    if request.user.is_authenticated:
        return redirect("/shop/")
    if request.method == "GET":
        return render(request, "shop/register.html", {"error": ""})

    username = (request.POST.get("username") or "").strip()
    password = (request.POST.get("password") or "").strip()
    confirm = (request.POST.get("confirm") or "").strip()

    if not username or not password or not confirm:
        return render(
            request,
            "shop/register.html",
            {"error": "Username, password, and confirmation are required."},
        )
    if password != confirm:
        return render(
            request, "shop/register.html", {"error": "Passwords do not match."}
        )
    if User.objects.filter(username=username).exists():
        return render(
            request, "shop/register.html", {"error": "That username is already taken."}
        )

    with transaction.atomic():
        user = User.objects.create_user(username=username, password=password)
        _ensure_customer_role(user)
        company = Company.objects.create(name=f"{username} company", owner=user)
        UserCompanyProfile.objects.create(user=user, company=company)
        Customer.objects.create(
            company=company,
            user=user,
            name=username[:100],
            contact_name=username[:100],
            phone=username[:30],
            shop_name=username[:200],
            address="To be completed",
            delivery_zone="Unassigned",
            customer_level=Customer.Level.C,
            allow_credit=False,
            credit_limit=0.0,
            current_debt=0.0,
            minimum_order_amount=0.0,
            payment_cycle=Customer.PaymentCycle.CASH,
            account_status=Customer.AccountStatus.APPROVED,
            is_active=True,
        )
        auth_login(request, user)
    return redirect("/shop/")


@require_GET
def shop_login(request):
    return redirect("/login/?next=/shop/")


@require_GET
def shop_logout(request):
    return redirect("/logout/?next=/shop/")


def login_view(request):
    if request.user.is_authenticated:
        return redirect("shop-home")
    if request.method == "GET":
        return render(request, "shop/login.html")

    username = (request.POST.get("username") or "").strip()
    password = (request.POST.get("password") or "").strip()
    user = authenticate(request, username=username, password=password)
    if not user:
        messages.error(request, "Invalid username or password.")
        return render(request, "shop/login.html")
    auth_login(request, user)
    _ensure_customer_role(user)
    if not (user.is_staff or user.is_superuser):
        ensure_customer_profile(user)
    next_url = (request.POST.get("next") or "").strip()
    if next_url:
        return redirect(next_url)
    return redirect("shop-home")


@login_required
def logout_view(request):
    auth_logout(request)
    return redirect("login")


@login_required
@require_http_methods(["GET", "POST"])
def profile_view(request):
    customer = get_shop_customer(request)
    if not customer:
        messages.info(
            request,
            "No shop customer profile for this account. Staff: manage customers in Admin.",
        )
        return redirect("shop-home")
    if request.method == "POST":
        customer.contact_name = (
            request.POST.get("contact_name") or customer.contact_name or customer.name
        )[:100]
        customer.phone = (request.POST.get("phone") or customer.phone)[:30]
        customer.address = (request.POST.get("address") or customer.address)[:255]
        customer.save(update_fields=["contact_name", "phone", "address"])
        messages.success(request, "Saved.")
        return redirect("profile")
    current_debt = money_float(customer.current_debt or 0.0)
    return render(
        request,
        "shop/profile.html",
        {"shop_customer": customer, "current_debt": current_debt},
    )


@login_required(login_url="/login/")
def my_orders_view(request):
    customer = get_shop_customer(request)
    if not customer:
        messages.info(request, "No shop customer profile for this account.")
        return redirect("shop-home")
    orders = (
        Order.objects.filter(ordered_by_id=request.user.id, customer_id=customer.pk)
        .prefetch_related("items__product")
        .order_by("-created_at")
    )
    return render(
        request,
        "shop/my_orders.html",
        {"orders": orders, "shop_customer": customer},
    )


@login_required
def credit_apply_view(request):
    customer = get_shop_customer(request)
    if not customer:
        messages.info(request, "No shop customer profile for this account.")
        return redirect("shop-home")
    if request.method == "GET":
        latest = (
            CreditApplication.objects.filter(customer_id=customer.pk)
            .order_by("-created_at")
            .first()
        )
        return render(
            request,
            "shop/credit_apply.html",
            {"shop_customer": customer, "latest_application": latest},
        )

    monthly_purchase_estimate = float(
        request.POST.get("monthly_purchase_estimate") or 0
    )
    requested_credit_limit = float(request.POST.get("requested_credit_limit") or 0)
    if monthly_purchase_estimate <= 0 or requested_credit_limit <= 0:
        messages.error(
            request,
            "Monthly purchase amount and requested limit must be greater than 0.",
        )
        return redirect("credit-apply")

    CreditApplication.objects.create(
        customer=customer,
        shop_name=(request.POST.get("shop_name") or customer.shop_name or "")[:200],
        contact_name=(
            request.POST.get("contact_name")
            or customer.contact_name
            or customer.name
            or ""
        )[:100],
        phone=(request.POST.get("phone") or customer.phone or "")[:30],
        monthly_purchase_estimate=monthly_purchase_estimate,
        requested_credit_limit=requested_credit_limit,
        note=(request.POST.get("note") or "")[:2000],
        status=CreditApplication.Status.PENDING,
    )
    messages.success(request, "Credit application submitted. Pending approval.")
    return redirect("credit-home")


@login_required
def credit_home_view(request):
    customer = get_shop_customer(request)
    if not customer:
        messages.info(request, "No shop customer profile for this account.")
        return redirect("shop-home")
    current_debt = money_float(customer.current_debt or 0.0)
    latest = (
        CreditApplication.objects.filter(customer_id=customer.pk)
        .order_by("-created_at")
        .first()
    )
    return render(
        request,
        "shop/credit_home.html",
        {
            "shop_customer": customer,
            "current_debt": current_debt,
            "used_credit": current_debt,
            "remaining_credit": money_float(
                max(
                    money_dec(0),
                    money_dec(customer.credit_limit or 0.0) - money_dec(current_debt),
                )
            ),
            "latest_application": latest,
        },
    )


@require_GET
def shop_checkout(request):
    if not request.user.is_authenticated:
        return redirect(f"/login/?next={request.path}")
    customer = get_shop_customer(request)
    can_order, order_hint = shop_order_permission(customer)
    categories = ProductCategory.objects.filter(is_active=True).order_by(
        "sort_order", "id"
    )
    products = (
        Product.objects.filter(is_active=True)
        .select_related("category", "ingredient")
        .order_by("category__sort_order", "category_id", "name")
    )
    shop_items = [_shop_product_row(customer, p) for p in products]
    return render(
        request,
        "shop/shop_checkout.html",
        {
            "shop_customer": customer,
            "shop_products": shop_items,
            "categories": categories,
            "shop_can_order": can_order,
            "shop_order_block_hint": order_hint,
            "tier_rules_banner": tier_rules_banner_text(),
            "credit_allow": bool(customer and customer.allow_credit),
            "credit_limit": (
                money_float(customer.credit_limit or 0.0) if customer else 0.0
            ),
            "current_debt": (
                money_float(customer.current_debt or 0.0) if customer else 0.0
            ),
        },
    )


@require_GET
def shop_product_detail(request, product_id):
    customer = get_shop_customer(request)
    p = get_object_or_404(
        Product.objects.select_related("category", "ingredient"), pk=product_id
    )
    if not p.is_active:
        messages.error(request, "This product is unavailable.")
        return redirect("shop-home")
    row = _shop_product_row(customer, p)
    can_order, order_hint = shop_order_permission(customer)
    return render(
        request,
        "shop/shop_product_detail.html",
        {
            "product_row": row,
            "shop_customer": customer,
            "shop_can_order": can_order,
            "shop_order_block_hint": order_hint,
        },
    )


@require_GET
def shop_order_success(request, order_id):
    order = get_object_or_404(
        Order.objects.prefetch_related("items__product"), pk=order_id
    )
    customer = get_shop_customer(request)
    if not customer or order.customer_id != customer.pk:
        messages.error(request, "You are not allowed to view this order.")
        return redirect("shop-home")
    if not request.user.is_authenticated or order.ordered_by_id != request.user.id:
        messages.error(request, "You are not allowed to view this order.")
        return redirect("shop-home")
    bank = getattr(settings, "BANK_TRANSFER_INFO", None) or {}
    return render(
        request, "shop/shop_order_success.html", {"order": order, "bank_info": bank}
    )


@require_GET
def shop_orders(request):
    customer = get_shop_customer(request)
    if not customer:
        messages.error(request, "Sign in with a customer account to view orders.")
        return redirect("shop-home")
    orders = (
        Order.objects.filter(ordered_by_id=request.user.id, customer_id=customer.pk)
        .prefetch_related("items__product")
        .order_by("-created_at")
    )
    return render(
        request,
        "shop/shop_orders.html",
        {"orders": orders, "shop_customer": customer},
    )


@require_POST
def shop_submit_order(request):
    if not request.user.is_authenticated:
        messages.error(request, "Sign in with a customer account to submit an order.")
        next_path = (
            "/checkout/"
            if (request.POST.get("next") or "").strip() == "checkout"
            else "/shop/"
        )
        return redirect(f"/login/?next={next_path}")
    customer = get_shop_customer(request)
    can_order, hint = shop_order_permission(customer)
    if not can_order:
        messages.error(request, hint)
        if (request.POST.get("next") or "").strip() == "checkout":
            return redirect("shop-checkout")
        return redirect("shop-home")

    lines_raw = request.POST.get("lines_json", "[]")
    submit_extra = {
        "contact_name": request.POST.get("contact_name", ""),
        "delivery_phone": request.POST.get("delivery_phone", ""),
        "store_name": request.POST.get("store_name", ""),
        "delivery_address": request.POST.get("delivery_address", ""),
        "order_note": request.POST.get("order_note", ""),
        "check_number": request.POST.get("check_number", ""),
        "settlement_type": request.POST.get(
            "settlement_type", Order.SettlementType.CASH
        ),
        "payment_method": request.POST.get(
            "payment_method", Order.PaymentMethod.BANK_TRANSFER
        ),
        "transfer_reference": request.POST.get("transfer_reference", ""),
    }
    sig_id = str(customer.pk)
    sig = _make_order_submit_signature(
        prefix="shop",
        customer_id=sig_id,
        lines_json=str(lines_raw),
        extra=submit_extra,
    )
    lock_key = f"submit_lock::shop::{sig_id}"
    if _check_and_set_submit_lock(request=request, lock_key=lock_key, signature=sig):
        messages.error(
            request, "Duplicate submit detected. Please do not click confirm twice."
        )
        if (request.POST.get("next") or "").strip() == "checkout":
            return redirect("shop-checkout")
        return redirect("shop-home")

    fail_redirect = (
        "shop-checkout"
        if (request.POST.get("next") or "").strip() == "checkout"
        else "shop-home"
    )

    try:
        lines = json.loads(lines_raw)
        shipping = {
            "contact_name": request.POST.get("contact_name", ""),
            "delivery_phone": request.POST.get("delivery_phone", ""),
            "store_name": request.POST.get("store_name", ""),
            "delivery_address": request.POST.get("delivery_address", ""),
            "order_note": request.POST.get("order_note", ""),
            "check_number": request.POST.get("check_number", ""),
            "settlement_type": request.POST.get(
                "settlement_type", Order.SettlementType.CASH
            ),
            "payment_method": request.POST.get(
                "payment_method", Order.PaymentMethod.BANK_TRANSFER
            ),
            "transfer_reference": request.POST.get("transfer_reference", ""),
        }
        order = submit_order_from_lines(
            request,
            customer,
            lines,
            from_shop=True,
            shipping=shipping,
            guest_session_key=None,
        )
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.success(
            request,
            "Order submitted. Please wait for payment confirmation.",
        )
        return redirect("shop-order-success", order_id=order.id)
    except (ValidationError, ValueError, TypeError) as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, str(exc))
    except json.JSONDecodeError as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, "Invalid order data.")
    except Product.DoesNotExist as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, "Product not found. Refresh and try again.")
    except Exception as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, f"Submit failed: {exc}")
    return redirect(fail_redirect)


@login_required
@require_POST
def checkout_submit_order(request):
    """Checkout 专用提交入口，复用商城下单逻辑。"""
    return shop_submit_order(request)


def _sales_units_by_product(days):
    """按订单明细汇总出库单位销量（排除待确认、已取消）。"""
    since = timezone.now() - timedelta(days=days)
    m = defaultdict(float)
    qs = (
        OrderItem.objects.filter(order__created_at__gte=since)
        .exclude(
            order__workflow_status__in=[
                Order.WorkflowStatus.PENDING_CONFIRM,
                Order.WorkflowStatus.CONFIRMED,
                Order.WorkflowStatus.CANCELLED,
            ]
        )
        .select_related("product")
    )
    for it in qs.iterator():
        m[it.product_id] += float(_stock_need_for_line(it, it.product))
    return m


def _replenishment_risk_and_action(stock, sold_30d, days_cover):
    """
    补货预警中心 — 库存风险等级规则（与模板展示一致）：

    - 近30天销量为 0：返回 observe，不进入红/黄/绿，避免误判为高风险。
    - 红色：预计可卖天数 <= 7，或 当前库存 < 近30天销量的 25%
    - 黄色：非红色，且（预计可卖天数 <= 15 或 当前库存 < 近30天销量的 50%）
    - 绿色：其余正常

    建议动作文案由模板侧展示为：红→立即补货（重要）；黄→尽快补货；绿→库存安全；observe→暂无销量，继续观察
    """
    if sold_30d <= 1e-12:
        return {
            "level": "observe",
            "label": "Observing",
            "action": "No sales yet — keep watching",
        }

    is_red = False
    if days_cover is not None and days_cover <= 7:
        is_red = True
    if stock < 0.25 * sold_30d:
        is_red = True

    if is_red:
        return {
            "level": "red",
            "label": "Red alert",
            "action": "Restock now (important)",
        }

    is_yellow = False
    if days_cover is not None and days_cover <= 15:
        is_yellow = True
    if stock < 0.5 * sold_30d:
        is_yellow = True

    if is_yellow:
        return {"level": "yellow", "label": "Yellow watch", "action": "Restock soon"}

    return {"level": "green", "label": "Stock OK", "action": "Stock OK"}


@login_required
@boss_required
def replenishment_dashboard(request):
    """
    老板决策版补货预警：风险分级、排序、汇总、建议采购量（60 天需求 − 当前库存）。
    """
    sold_7_map = _sales_units_by_product(7)
    sold_30_map = _sales_units_by_product(30)

    products = Product.objects.select_related("category", "ingredient").order_by(
        "category", "name"
    )
    rows = []
    for p in products:
        s7 = float(sold_7_map.get(p.id, 0.0))
        s30 = float(sold_30_map.get(p.id, 0.0))
        daily_avg = s30 / 30.0 if s30 > 1e-12 else 0.0
        if p.ingredient_id:
            stock = float(p.ingredient.stock)
        else:
            stock = float(p.stock_quantity)
        days_cover = (stock / daily_avg) if daily_avg > 1e-12 else None
        ra = _replenishment_risk_and_action(stock, s30, days_cover)
        level = ra["level"]
        if level == "observe":
            suggest_60 = 0.0
            sales_note = "No sales data yet"
        else:
            suggest_60 = max(0.0, 60.0 * daily_avg - stock)
            sales_note = ""

        # 预计可卖天数列：纯数字层面的档位（用于红字/橙字，与风险等级独立计算）
        days_cover_ui = "na"
        if not sales_note and days_cover is not None:
            if days_cover <= 7:
                days_cover_ui = "critical"
            elif days_cover <= 15:
                days_cover_ui = "warn"
            else:
                days_cover_ui = "ok"

        rows.append(
            {
                "product": p,
                "stock": stock,
                "sold_7d": s7,
                "sold_30d": s30,
                "daily_avg": daily_avg,
                "days_cover": days_cover,
                "days_cover_ui": days_cover_ui,
                "suggest_60": suggest_60,
                "risk_level": level,
                "risk_label": ra["label"],
                "suggest_action": ra["action"],
                "sales_ref_note": sales_note,
            }
        )

    level_order = {"red": 0, "yellow": 1, "green": 2, "observe": 3}

    def _row_sort_key(r):
        lv = level_order[r["risk_level"]]
        if r["risk_level"] == "observe":
            return (lv, float("inf"), r["product"].sku or "")
        dc = r["days_cover"]
        d = float(dc) if dc is not None else float("inf")
        return (lv, d, r["product"].sku or "")

    rows.sort(key=_row_sort_key)

    stats = {
        "total": len(rows),
        "red": sum(1 for r in rows if r["risk_level"] == "red"),
        "yellow": sum(1 for r in rows if r["risk_level"] == "yellow"),
        "immediate": sum(1 for r in rows if r["risk_level"] == "red"),
    }

    if stats["red"] > 0:
        rep_banner = {
            "kind": "danger",
            "text": "⚠️ Some items are critically low — prioritize restocking.",
        }
    elif stats["yellow"] > 0:
        rep_banner = {
            "kind": "warn",
            "text": "Some items should be restocked soon — check yellow alerts.",
        }
    else:
        rep_banner = {"kind": "safe", "text": "Overall stock levels look healthy."}

    return render(
        request,
        "replenishment.html",
        {"rows": rows, "stats": stats, "rep_banner": rep_banner},
    )


@require_GET
def demo_landing(request):
    """对外展示用 Demo 落地页（不参与业务逻辑）。"""
    return render(request, "demo_landing.html")


@login_required
@boss_required
def inventory_list(request):
    company = get_user_company(request.user)
    products = Product.objects.order_by("name", "sku")
    if company is not None:
        products = products.filter(company=company)
    rows = []
    low_count = 0
    severe_count = 0
    for p in products:
        st = float(getattr(p, "stock", 0.0) or 0.0)
        wl = float(p.safety_stock or 0.0)
        is_severe = wl > 0 and st < (wl / 2.0)
        is_low = st < wl and not is_severe
        reorder_calc = calculate_reorder(p)
        if is_low:
            low_count += 1
        if is_severe:
            severe_count += 1
        status_label = "OK"
        status_level = "ok"
        if is_severe:
            status_label = "Severe"
            status_level = "severe"
        elif is_low:
            status_label = "Low"
            status_level = "low"
        rows.append(
            {
                "product": p,
                "sku": p.sku,
                "name": p.name,
                "current_stock": st,
                "safety_stock": wl,
                "low_stock": is_low,
                "severe_stock": is_severe,
                "reorder_qty": float(reorder_calc["reorder_qty"] or 0.0),
                "reorder_reason": reorder_calc["reason"],
                "status_label": status_label,
                "status_level": status_level,
            }
        )
    total_count = len(rows)
    normal_count = total_count - low_count - severe_count
    stats = {
        "total": total_count,
        "low": low_count,
        "severe": severe_count,
        "normal": normal_count,
    }
    return render(request, "inventory.html", {"rows": rows, "stats": stats})


@login_required
@internal_user_required
def orders_list(request):
    company = get_user_company(request.user)
    unsettled_items = OrderItem.objects.filter(
        order__status__in=_unsettled_order_statuses()
    ).select_related("order__customer", "product")
    if company is not None:
        unsettled_items = unsettled_items.filter(order__company=company)

    amount_by_customer_id = defaultdict(lambda: money_dec(0))
    for item in unsettled_items:
        amount_by_customer_id[item.order.customer_id] += money_dec(item.total_revenue)

    customer_ids = [k for k in amount_by_customer_id.keys() if k is not None]
    customers_map = {c.id: c for c in Customer.objects.filter(pk__in=customer_ids)}

    earliest_pending = (
        Order.objects.filter(status=Order.Status.PENDING)
        .values("customer_id")
        .annotate(m=Min("created_at"))
    )
    if company is not None:
        earliest_pending = earliest_pending.filter(company=company)
    earliest_map = {row["customer_id"]: row["m"] for row in earliest_pending}

    customer_debts = []
    for cid, amount in sorted(amount_by_customer_id.items(), key=lambda x: -x[1]):
        if cid is None:
            name = "No customer"
            credit_limit = 0.0
            tier_label = "—"
        else:
            cust = customers_map[cid]
            name = cust.name
            credit_limit = money_float(cust.credit_limit)
            tier_label = cust.get_customer_level_display()
        days = _days_since_earliest_pending(earliest_map.get(cid))
        ds = _dunning_time_and_supply(money_float(amount), credit_limit, days)
        customer_debts.append(
            {
                "name": name,
                "tier": tier_label,
                "amount": money_float(amount),
                "credit_limit": credit_limit,
                "no_limit": ds["no_limit"],
                "dunning_status": ds["dunning_status"],
                "supply_advice": ds["supply_advice"],
                "dunning_style": ds["style"],
            }
        )

    orders_qs = (
        Order.objects.select_related("customer")
        .prefetch_related("items__product")
        .order_by("-created_at")
    )
    wf = (request.GET.get("workflow_status") or "").strip()
    st = (request.GET.get("status") or "").strip()
    q = (request.GET.get("q") or "").strip()
    cust_id = (request.GET.get("customer_id") or "").strip()
    date_from = (request.GET.get("date_from") or "").strip()
    date_to = (request.GET.get("date_to") or "").strip()

    if wf in dict(Order.WorkflowStatus.choices):
        orders_qs = orders_qs.filter(workflow_status=wf)
    if st in dict(Order.Status.choices):
        orders_qs = orders_qs.filter(status=st)
    if q:
        cond = (
            Q(customer__name__icontains=q)
            | Q(customer__phone__icontains=q)
            | Q(name__icontains=q)
            | Q(contact_name__icontains=q)
            | Q(delivery_phone__icontains=q)
            | Q(store_name__icontains=q)
            | Q(delivery_address__icontains=q)
        )
        if q.isdigit():
            cond |= Q(pk=int(q))
        orders_qs = orders_qs.filter(cond)
    if cust_id.isdigit():
        orders_qs = orders_qs.filter(customer_id=int(cust_id))
    if date_from:
        orders_qs = orders_qs.filter(created_at__date__gte=date_from)
    if date_to:
        orders_qs = orders_qs.filter(created_at__date__lte=date_to)

    orders = orders_qs

    today = timezone.localdate()
    today_agg = Order.objects.filter(created_at__date=today).aggregate(
        tr=Sum("total_revenue"),
        tc=Sum("total_cost"),
        tp=Sum("profit"),
    )
    today_stats = {
        "revenue": money_float(today_agg["tr"] or 0),
        "cost": money_float(today_agg["tc"] or 0),
        "profit": money_float(today_agg["tp"] or 0),
    }

    order_rows = []
    for order in orders:
        items = list(order.items.select_related("product").all())
        item_count = len(items)
        product_names = [it.product.name for it in items]
        preview = "、".join(product_names[:2])
        if item_count > 2:
            preview = f"{preview}, …"
        if not preview:
            preview = "-"
        amount = money_float(order.total_revenue or 0.0)
        cost = money_float(order.total_cost or 0.0)
        profit = money_float(order.profit or 0.0)
        profit_rate = (profit / amount * 100.0) if amount > 0 else None
        order_rows.append(
            {
                "order": order,
                "item_count": item_count,
                "items_text": f"{item_count} items" if item_count else "0 items",
                "product_preview": preview,
                "amount": amount,
                "cost": cost,
                "profit": profit,
                "profit_rate": profit_rate,
                "cost_missing": cost <= 0,
            }
        )

    return render(
        request,
        "orders_list.html",
        {
            "order_rows": order_rows,
            "customer_debts": customer_debts,
            "today_stats": today_stats,
            "filter_workflow": wf,
            "filter_status": st,
            "filter_q": q,
            "filter_customer_id": cust_id,
            "filter_date_from": date_from,
            "filter_date_to": date_to,
            "workflow_choices": Order.WorkflowStatus.choices,
            "status_choices": Order.Status.choices,
            "filter_customers": Customer.objects.all().order_by("name"),
        },
    )


def _customer_risk_status_label(customer):
    """风险展示优先级：已停单 > 超额度 > 关注 > 正常。"""
    debt = float(customer.current_debt or 0)
    limit = float(customer.credit_limit or 0)
    if customer.is_blocked:
        return "Suspended"
    if limit > 0 and debt >= limit:
        return "Over limit"
    if limit > 0 and debt / limit >= 0.8:
        return "Watch"
    return "OK"


@login_required
@internal_user_required
def customer_insights_dashboard(request):
    """
    客户价值与风险看板（稳版）：只读统计，口径与基础报表一致
    （排除 workflow_status=已取消；无客户订单不参与排行）。
    """
    valid_order_base = Order.objects.exclude(
        workflow_status=Order.WorkflowStatus.CANCELLED
    ).exclude(customer__isnull=True)

    profit_top = list(
        valid_order_base.values("customer_id", "customer__name")
        .annotate(
            order_count=Count("id"),
            total_sales=Coalesce(Sum("total_revenue"), 0.0),
            total_cost=Coalesce(Sum("total_cost"), 0.0),
            total_profit=Coalesce(Sum("profit"), 0.0),
        )
        .order_by("-total_profit")[:10]
    )
    profit_top = [
        {
            **r,
            "total_sales": money_float(r["total_sales"]),
            "total_cost": money_float(r["total_cost"]),
            "total_profit": money_float(r["total_profit"]),
        }
        for r in profit_top
    ]

    sales_top = list(
        valid_order_base.values("customer_id", "customer__name")
        .annotate(
            order_count=Count("id"),
            total_sales=Coalesce(Sum("total_revenue"), 0.0),
        )
        .order_by("-total_sales")[:10]
    )
    sales_top = [{**r, "total_sales": money_float(r["total_sales"])} for r in sales_top]

    debt_top = []
    for c in Customer.objects.all().order_by("-current_debt")[:10]:
        debt = money_float(c.current_debt or 0)
        limit = money_float(c.credit_limit or 0)
        ratio_pct = (debt / limit * 100.0) if limit > 0 else None
        debt_top.append(
            {
                "name": c.name,
                "current_debt": debt,
                "credit_limit": limit,
                "ratio_pct": ratio_pct,
                "is_blocked": c.is_blocked,
            }
        )

    risk_qs = Customer.objects.filter(
        Q(is_blocked=True)
        | Q(credit_limit__gt=0, current_debt__gte=F("credit_limit") * 0.8)
    ).order_by("-current_debt")
    risk_rows = []
    for c in risk_qs:
        risk_rows.append(
            {
                "name": c.name,
                "current_debt": money_float(c.current_debt or 0),
                "credit_limit": money_float(c.credit_limit or 0),
                "risk_status": _customer_risk_status_label(c),
            }
        )

    kpi_total_customers = Customer.objects.count()
    kpi_customers_with_orders = (
        valid_order_base.values("customer_id").distinct().count()
    )
    kpi_blocked_customers = Customer.objects.filter(is_blocked=True).count()
    kpi_customers_with_debt = Customer.objects.filter(current_debt__gt=0).count()

    return render(
        request,
        "reports_customer_insights.html",
        {
            "profit_top": profit_top,
            "sales_top": sales_top,
            "debt_top": debt_top,
            "risk_rows": risk_rows,
            "kpi_total_customers": kpi_total_customers,
            "kpi_customers_with_orders": kpi_customers_with_orders,
            "kpi_blocked_customers": kpi_blocked_customers,
            "kpi_customers_with_debt": kpi_customers_with_debt,
        },
    )


@login_required
@internal_user_required
def reports_dashboard(request):
    """基础报表：销售/利润概览 + 客户/商品 TOP10。"""
    today = timezone.localdate()
    month_start = today.replace(day=1)

    valid_orders = Order.objects.exclude(workflow_status=Order.WorkflowStatus.CANCELLED)
    today_orders = valid_orders.filter(created_at__date=today)
    month_orders = valid_orders.filter(
        created_at__date__gte=month_start, created_at__date__lte=today
    )

    today_agg = today_orders.aggregate(
        sales=Coalesce(Sum("total_revenue"), 0.0),
        profit=Coalesce(Sum("profit"), 0.0),
    )
    month_agg = month_orders.aggregate(
        sales=Coalesce(Sum("total_revenue"), 0.0),
        profit=Coalesce(Sum("profit"), 0.0),
    )
    pending_confirm_count = Order.objects.filter(
        workflow_status=Order.WorkflowStatus.PENDING_CONFIRM
    ).count()
    confirmed_unpaid_amount = (
        Order.objects.filter(
            workflow_status=Order.WorkflowStatus.CONFIRMED, status=Order.Status.PENDING
        )
        .aggregate(v=Coalesce(Sum("total_revenue"), 0.0))
        .get("v", 0.0)
    )

    customer_sales_top = (
        Order.objects.exclude(workflow_status=Order.WorkflowStatus.CANCELLED)
        .exclude(customer__isnull=True)
        .values("customer_id", "customer__name")
        .annotate(value=Coalesce(Sum("total_revenue"), 0.0))
        .order_by("-value")[:10]
    )
    customer_profit_top = (
        Order.objects.exclude(workflow_status=Order.WorkflowStatus.CANCELLED)
        .exclude(customer__isnull=True)
        .values("customer_id", "customer__name")
        .annotate(value=Coalesce(Sum("profit"), 0.0))
        .order_by("-value")[:10]
    )
    customer_debt_top = (
        Customer.objects.values("id", "name")
        .annotate(value=Coalesce(F("current_debt"), 0.0))
        .order_by("-value")[:10]
    )

    product_base = OrderItem.objects.exclude(
        order__workflow_status=Order.WorkflowStatus.CANCELLED
    ).values("product_id", "product__name", "product__sku")
    product_qty_top = product_base.annotate(
        value=Coalesce(Sum("quantity"), 0.0)
    ).order_by("-value")[:10]
    product_sales_top = product_base.annotate(
        value=Coalesce(Sum("total_revenue"), 0.0)
    ).order_by("-value")[:10]
    product_profit_top = product_base.annotate(
        value=Coalesce(Sum("profit"), 0.0)
    ).order_by("-value")[:10]
    negative_profit_orders = list(
        Order.objects.exclude(workflow_status=Order.WorkflowStatus.CANCELLED)
        .filter(profit__lt=0)
        .select_related("customer")
        .order_by("profit", "-created_at")[:10]
    )
    abnormal_profit_order_count = (
        Order.objects.exclude(workflow_status=Order.WorkflowStatus.CANCELLED)
        .filter(profit__lt=0)
        .count()
    )
    missing_cost_q = Q(cost_price_single__lte=0) | Q(cost_price_case__lte=0)
    missing_cost_products = list(
        Product.objects.filter(missing_cost_q)
        .values("id", "name", "sku", "cost_price_single", "cost_price_case")
        .order_by("sku", "id")[:10]
    )
    missing_cost_product_count = Product.objects.filter(missing_cost_q).count()

    def _annotate_value_money(rows):
        return [{**r, "value": money_float(r["value"])} for r in rows]

    context = {
        "kpi_today_sales": money_float(today_agg["sales"] or 0.0),
        "kpi_today_profit": money_float(today_agg["profit"] or 0.0),
        "kpi_pending_confirm_count": int(pending_confirm_count),
        "kpi_confirmed_unpaid_amount": money_float(confirmed_unpaid_amount or 0.0),
        "kpi_month_sales": money_float(month_agg["sales"] or 0.0),
        "kpi_month_profit": money_float(month_agg["profit"] or 0.0),
        "customer_sales_top": _annotate_value_money(customer_sales_top),
        "customer_profit_top": _annotate_value_money(customer_profit_top),
        "customer_debt_top": _annotate_value_money(customer_debt_top),
        "product_qty_top": _annotate_value_money(product_qty_top),
        "product_sales_top": _annotate_value_money(product_sales_top),
        "product_profit_top": _annotate_value_money(product_profit_top),
        "abnormal_profit_order_count": int(abnormal_profit_order_count),
        "negative_profit_orders": negative_profit_orders,
        "missing_cost_product_count": int(missing_cost_product_count),
        "missing_cost_products": missing_cost_products,
    }
    return render(request, "reports_basic.html", context)


@login_required
@boss_required
def boss_dashboard(request):
    """Business control center for owner (B2B/ERP-style)."""
    company = get_user_company(request.user)
    today = timezone.localdate()

    valid_orders = Order.objects.exclude(workflow_status=Order.WorkflowStatus.CANCELLED)
    if company is not None:
        valid_orders = valid_orders.filter(company=company)
    today_orders = valid_orders.filter(created_at__date=today)
    today_agg = today_orders.aggregate(
        sales=Coalesce(Sum("total_revenue"), 0.0),
        cost=Coalesce(Sum("total_cost"), 0.0),
        profit=Coalesce(Sum("profit"), 0.0),
    )

    kpi_today_order_count = int(today_orders.count())
    kpi_today_sales = money_float(today_agg["sales"] or 0.0)
    kpi_today_profit = money_float(today_agg["profit"] or 0.0)
    kpi_today_cost = money_float(today_agg["cost"] or 0.0)
    kpi_today_margin_pct = (
        0.0
        if float(kpi_today_sales or 0.0) <= 0
        else money_float((float(kpi_today_profit or 0.0) / float(kpi_today_sales or 1.0)) * 100.0)
    )

    unpaid_qs = Order.objects.filter(status=Order.Status.PENDING).exclude(
        workflow_status=Order.WorkflowStatus.CANCELLED
    )
    if company is not None:
        unpaid_qs = unpaid_qs.filter(company=company)
    kpi_unpaid_order_count = int(unpaid_qs.count())

    kpi_dispatch_assigned_count = int(
        (Order.objects.filter(delivery_status="assigned", company=company).count()
         if company is not None
         else Order.objects.filter(delivery_status="assigned").count())
    )
    kpi_dispatch_delivering_count = int(
        (Order.objects.filter(delivery_status="delivering", company=company).count()
         if company is not None
         else Order.objects.filter(delivery_status="delivering").count())
    )

    recent_qs = Order.objects.select_related(
        "customer", "assigned_driver", "assigned_vehicle"
    ).order_by("-created_at")
    if company is not None:
        recent_qs = recent_qs.filter(company=company)
    recent_orders = list(recent_qs[:10])
    unpaid_orders = list(
        unpaid_qs.select_related("customer").order_by("-created_at")[:10]
    )
    dispatch_orders = list(
        Order.objects.filter(delivery_status__in=["assigned", "delivering"])
        .select_related("customer", "assigned_driver", "assigned_vehicle")
        .order_by("-created_at")[:10]
    )
    if company is not None:
        dispatch_orders = list(
            Order.objects.filter(
                company=company, delivery_status__in=["assigned", "delivering"]
            )
            .select_related("customer", "assigned_driver", "assigned_vehicle")
            .order_by("-created_at")[:10]
        )

    customer_profit_top5 = list(
        valid_orders.exclude(customer__isnull=True)
        .values("customer_id", "customer__name")
        .annotate(
            revenue=Coalesce(Sum("total_revenue"), 0.0),
            profit=Coalesce(Sum("profit"), 0.0),
            orders=Coalesce(Count("id"), 0),
        )
        .order_by("-profit")[:5]
    )
    customer_profit_top5 = [
        {
            **r,
            "revenue": money_float(r["revenue"] or 0.0),
            "profit": money_float(r["profit"] or 0.0),
            "orders": int(r["orders"] or 0),
        }
        for r in customer_profit_top5
    ]

    product_items = OrderItem.objects.exclude(
        order__workflow_status=Order.WorkflowStatus.CANCELLED
    )
    if company is not None:
        product_items = product_items.filter(order__company=company)
    product_profit_base = (
        product_items.values("product_id", "product__name", "product__sku")
        .annotate(
            qty=Coalesce(Sum("quantity"), 0.0),
            profit=Coalesce(Sum("profit"), 0.0),
        )
        .order_by("-profit")[:5]
    )
    product_profit_top5 = [
        {
            "product_id": r["product_id"],
            "name": r["product__name"],
            "sku": r["product__sku"],
            "qty": float(r["qty"] or 0.0),
            "profit": money_float(r["profit"] or 0.0),
            "unit_profit": (
                0.0
                if float(r["qty"] or 0.0) <= 0
                else money_float(float(r["profit"] or 0.0) / float(r["qty"] or 1.0))
            ),
        }
        for r in product_profit_base
    ]

    recommendation_rows = _profit_recommendation_rows(company=company, limit=5)

    # 7-day trend (orders + sales)
    start = today - timedelta(days=6)
    trend_qs = (
        valid_orders.filter(created_at__date__gte=start, created_at__date__lte=today)
        .annotate(d=TruncDate("created_at"))
        .values("d")
        .annotate(
            orders=Coalesce(Count("id"), 0),
            sales=Coalesce(Sum("total_revenue"), 0.0),
        )
        .order_by("d")
    )
    trend_map = {r["d"]: r for r in trend_qs}
    trend = []
    for i in range(7):
        d = start + timedelta(days=i)
        row = trend_map.get(d, {"orders": 0, "sales": 0.0})
        trend.append(
            {
                "date": d,
                "orders": int(row.get("orders") or 0),
                "sales": money_float(row.get("sales") or 0.0),
            }
        )

    low_stock_rows = []
    for p in (
        Product.objects.filter(stock_enabled=True)
        .filter(stock__lt=F("safety_stock"))
        .order_by("stock", "sku")[:5]
    ):
        st = float(getattr(p, "stock", 0.0) or 0.0)
        wl = float(getattr(p, "safety_stock", 0.0) or 0.0)
        is_severe = wl > 0 and st < (wl / 2.0)
        low_stock_rows.append(
            {
                "product": p,
                "sku": p.sku,
                "name": p.name,
                "stock": st,
                "safety_stock": wl,
                "is_severe": is_severe,
            }
        )

    reorder_rows = []
    for p in Product.objects.filter(stock_enabled=True).order_by("sku", "id"):
        st = float(getattr(p, "stock", 0.0) or 0.0)
        wl = float(getattr(p, "safety_stock", 0.0) or 0.0)
        calc = calculate_reorder(p)
        reorder_qty = float(calc["reorder_qty"] or 0.0)
        if reorder_qty <= 0:
            continue
        is_urgent = wl > 0 and st < (wl / 2.0)
        status = "Reorder"
        if is_urgent:
            status = "Urgent"
        reorder_rows.append(
            {
                "product": p,
                "sku": p.sku,
                "name": p.name,
                "stock": st,
                "safety_stock": wl,
                "demand": float(calc["demand"] or 0.0),
                "target_stock": float(calc["target_stock"] or 0.0),
                "reorder_qty": reorder_qty,
                "status": status,
                "reason": calc["reason"],
            }
        )
    reorder_rows.sort(
        key=lambda r: (
            0 if r["status"] == "Urgent" else (1 if r["status"] == "Reorder" else 2),
            -float(r["reorder_qty"] or 0),
            str(r["sku"] or ""),
        )
    )
    reorder_rows = reorder_rows[:5]

    vip_count = int(Customer.objects.filter(level=Customer.ValueLevel.VIP).count())
    premium_count = int(
        Customer.objects.filter(level=Customer.ValueLevel.PREMIUM).count()
    )

    return render(
        request,
        "boss_dashboard.html",
        {
            "kpi_today_order_count": kpi_today_order_count,
            "kpi_today_sales": kpi_today_sales,
            "kpi_today_cost": kpi_today_cost,
            "kpi_today_profit": kpi_today_profit,
            "kpi_today_margin_pct": kpi_today_margin_pct,
            "kpi_unpaid_order_count": kpi_unpaid_order_count,
            "kpi_dispatch_assigned_count": kpi_dispatch_assigned_count,
            "kpi_dispatch_delivering_count": kpi_dispatch_delivering_count,
            "recent_orders": recent_orders,
            "unpaid_orders": unpaid_orders,
            "dispatch_orders": dispatch_orders,
            "customer_profit_top5": customer_profit_top5,
            "product_profit_top5": product_profit_top5,
            "recommendation_rows": recommendation_rows,
            "trend7": trend,
            "low_stock_rows": low_stock_rows,
            "reorder_rows": reorder_rows,
            "vip_count": vip_count,
            "premium_count": premium_count,
        },
    )


@login_required
@boss_required
@require_POST
def boss_start_delivery(request, order_id):
    """Owner action: assigned -> delivering (one-click from dashboard)."""
    with transaction.atomic():
        o = get_object_or_404(Order.objects.select_for_update(), pk=order_id)
        if o.delivery_status != "assigned":
            messages.error(request, "Cannot start delivery: order is not in 'assigned' state.")
            return redirect("boss-dashboard")
        o.delivery_status = "delivering"
        o.save(update_fields=["delivery_status"])
    messages.success(request, "Delivery started.")
    return redirect("boss-dashboard")


_MSG_SETTLED_RELEASE = (
    "Settlement recorded: this receivable is cleared and no longer uses credit limit. "
    "Customer debt and risk views are updated; you may place new orders if you were blocked by limit."
)


def _confirm_order_guard_reason(order):
    """
    确认订单后端保护：
    1) 未设置成本（总成本=0 或明细缺成本） -> 禁止确认
    2) 总利润<0 -> 禁止确认
    3) 利润率<5% -> 允许（前端提醒）
    """
    amount = money_dec(order.total_revenue or 0)
    total_cost = money_dec(0)
    has_missing_cost = False
    for item in order.items.select_related("product").all():
        p = item.product
        qty = money_dec(item.quantity or 0)
        if str(item.sale_type) == OrderItem.SaleType.CASE:
            unit_cost = money_dec(getattr(p, "cost_price_case", 0) or 0)
        else:
            unit_cost = money_dec(getattr(p, "cost_price_single", 0) or 0)
        if unit_cost <= money_dec(0):
            has_missing_cost = True
        total_cost += money_q2(unit_cost * qty)

    profit = money_q2(amount - total_cost)
    if has_missing_cost or total_cost <= money_dec(0):
        return "Cost not set; order cannot be confirmed."
    if profit < money_dec(0):
        return "Negative profit; order cannot be confirmed."
    return ""


@login_required
@internal_user_required
def mark_order_paid(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    if order.status == Order.Status.PAID:
        messages.info(request, "Order was already settled; credit usage unchanged.")
        return redirect("orders-list")
    try:
        with transaction.atomic():
            # 禁止 select_related 可空外键与 select_for_update 混用（PostgreSQL：FOR UPDATE 不能锁 outer join 可空侧）
            order = Order.objects.select_for_update().get(pk=order_id)
            if order.status == Order.Status.PAID:
                messages.info(request, "Order was already settled; credit usage unchanged.")
                return redirect("orders-list")
            deduct_stock_for_order(order.pk)
            order.status = Order.Status.PAID
            order.payment_status = Order.PaymentStatus.PAID
            order.paid_at = timezone.now()
            order.save(update_fields=["status", "payment_status", "paid_at"])
            reverse_credit_debt_if_counted(order)
            if order.customer_id:
                update_customer_level(order.customer)
    except ValidationError as exc:
        messages.error(request, str(exc))
        return redirect("orders-list")
    messages.success(request, _MSG_SETTLED_RELEASE)
    return redirect("orders-list")


@login_required
@internal_user_required
def confirm_order(request, order_id):
    try:
        with transaction.atomic():
            order = get_object_or_404(Order.objects.select_for_update(), pk=order_id)
            if order.workflow_status == Order.WorkflowStatus.CANCELLED:
                messages.error(request, "Cancelled orders cannot be confirmed.")
                return redirect("orders-list")
            guard_reason = _confirm_order_guard_reason(order)
            if guard_reason:
                messages.error(request, guard_reason)
                return redirect("orders-list")
            deduct_stock_for_order(order.pk)
            if order.workflow_status != Order.WorkflowStatus.CONFIRMED:
                order.workflow_status = Order.WorkflowStatus.CONFIRMED
                order.save(update_fields=["workflow_status"])
            apply_credit_debt_if_needed(order)
            if order.customer_id:
                update_customer_level(order.customer)
    except ValidationError as exc:
        messages.error(request, str(exc))
        return redirect("orders-list")
    messages.success(request, "Order confirmed.")
    return redirect("orders-list")


@login_required
@internal_user_required
def cancel_order(request, order_id):
    with transaction.atomic():
        order = get_object_or_404(Order.objects.select_for_update(), pk=order_id)
        order.workflow_status = Order.WorkflowStatus.CANCELLED
        order.save(update_fields=["workflow_status"])
        reverse_credit_debt_if_counted(order)
    messages.success(request, "Order cancelled.")
    return redirect("orders-list")


def _order_status_update_context(order):
    return {
        "order": order,
        "status_choices": Order.Status.choices,
        "workflow_choices": Order.WorkflowStatus.choices,
        "settlement_choices": Order.SettlementType.choices,
        "payment_method_choices": Order.PaymentMethod.choices,
        "payment_status_choices": Order.PaymentStatus.choices,
    }


@login_required
@internal_user_required
def order_status_update(request, order_id):
    """
    订单详情页保存入口：履约/结算方式/支付相关字段 + 结算状态（应收账款）。
    仅锁 Order 主表；联动欠款在同事务内执行，失败则整笔回滚并提示原因。
    """
    company = get_user_company(request.user)
    detail_qs = Order.objects.select_related("customer").prefetch_related("items__product")
    if company is not None:
        detail_qs = detail_qs.filter(company=company)

    if request.method == "POST":
        workflow = (request.POST.get("workflow_status") or "").strip()
        settlement = (request.POST.get("settlement_type") or "").strip()
        payment_method = (request.POST.get("payment_method") or "").strip()
        payment_status = (request.POST.get("payment_status") or "").strip()
        status = (request.POST.get("status") or "").strip()

        valid_wf = {c[0] for c in Order.WorkflowStatus.choices}
        valid_settlement = {c[0] for c in Order.SettlementType.choices}
        valid_pm = {c[0] for c in Order.PaymentMethod.choices}
        valid_ps = {c[0] for c in Order.PaymentStatus.choices}
        valid_status = {c[0] for c in Order.Status.choices}

        if not (
            workflow in valid_wf
            and settlement in valid_settlement
            and payment_method in valid_pm
            and payment_status in valid_ps
            and status in valid_status
        ):
            logger.warning(
                "order_status_update invalid POST order_id=%s wf=%r settlement=%r pm=%r ps=%r status=%r",
                order_id,
                workflow,
                settlement,
                payment_method,
                payment_status,
                status,
            )
            messages.error(
                request,
                "Save failed: workflow, settlement, payment method, payment status, or settlement status is invalid.",
            )
            order = get_object_or_404(detail_qs, pk=order_id)
            return render(
                request, "order_status_update.html", _order_status_update_context(order)
            )

        try:
            with transaction.atomic():
                order = Order.objects.select_for_update().get(pk=order_id)
                old_wf = order.workflow_status
                old_status = order.status
                old_snapshot = {
                    "workflow_status": order.workflow_status,
                    "settlement_type": order.settlement_type,
                    "payment_method": order.payment_method,
                    "payment_status": order.payment_status,
                    "status": order.status,
                    "paid_at": order.paid_at,
                }
                logger.info(
                    "order_status_update order_id=%s before wf=%s settlement=%s pm=%s ps=%s status=%s paid_at=%s",
                    order_id,
                    old_snapshot["workflow_status"],
                    old_snapshot["settlement_type"],
                    old_snapshot["payment_method"],
                    old_snapshot["payment_status"],
                    old_snapshot["status"],
                    old_snapshot["paid_at"],
                )

                order.workflow_status = workflow
                order.settlement_type = settlement
                order.payment_method = payment_method
                order.payment_status = payment_status
                order.status = status
                if (
                    status == Order.Status.PAID
                    and old_status != Order.Status.PAID
                    and order.paid_at is None
                ):
                    order.paid_at = timezone.now()

                update_fields = [
                    "workflow_status",
                    "settlement_type",
                    "payment_method",
                    "payment_status",
                    "status",
                ]
                if order.paid_at != old_snapshot["paid_at"]:
                    update_fields.append("paid_at")

                logger.info(
                    "order_status_update order_id=%s entering save() update_fields=%s new wf=%s settlement=%s pm=%s ps=%s status=%s paid_at=%s",
                    order_id,
                    update_fields,
                    workflow,
                    settlement,
                    payment_method,
                    payment_status,
                    status,
                    order.paid_at,
                )
                order.save(update_fields=update_fields)
                logger.info(
                    "order_status_update order_id=%s save() finished without DB error",
                    order_id,
                )

                if (
                    workflow == Order.WorkflowStatus.CONFIRMED
                    and old_wf != Order.WorkflowStatus.CONFIRMED
                ):
                    logger.info(
                        "order_status_update order_id=%s side_effect apply_credit_debt_if_needed",
                        order_id,
                    )
                    apply_credit_debt_if_needed(order)
                if (
                    workflow == Order.WorkflowStatus.CANCELLED
                    and old_wf != Order.WorkflowStatus.CANCELLED
                ):
                    logger.info(
                        "order_status_update order_id=%s side_effect reverse_credit_debt_if_counted (cancel)",
                        order_id,
                    )
                    reverse_credit_debt_if_counted(order)
                if status == Order.Status.PAID and old_status != Order.Status.PAID:
                    logger.info(
                        "order_status_update order_id=%s side_effect reverse_credit_debt_if_counted (paid)",
                        order_id,
                    )
                    reverse_credit_debt_if_counted(order)

            logger.info(
                "order_status_update order_id=%s transaction committed", order_id
            )
            if status == Order.Status.PAID and old_status != Order.Status.PAID:
                messages.success(request, _MSG_SETTLED_RELEASE)
            else:
                messages.success(request, "Order saved.")
            return redirect(
                reverse("order-status-update", kwargs={"order_id": order_id})
            )
        except ValidationError as exc:
            logger.exception(
                "order_status_update order_id=%s ValidationError transaction rolled back: %s",
                order_id,
                exc,
            )
            messages.error(request, f"Save failed (rolled back): {exc}")
        except Exception as exc:
            logger.exception(
                "order_status_update order_id=%s unexpected error transaction rolled back: %s",
                order_id,
                exc,
            )
            messages.error(request, f"Save failed (rolled back): {exc}")

        order = get_object_or_404(detail_qs, pk=order_id)
        return render(
            request, "order_status_update.html", _order_status_update_context(order)
        )

    order = get_object_or_404(detail_qs, pk=order_id)
    return render(
        request, "order_status_update.html", _order_status_update_context(order)
    )


def _stripe_secret_key():
    return (
        os.environ.get("STRIPE_SECRET_KEY")
        or getattr(settings, "STRIPE_SECRET_KEY", "")
        or ""
    ).strip()


def _bank_transfer_info():
    return {
        "bank_name": (
            os.environ.get("BANK_NAME") or getattr(settings, "BANK_NAME", "") or ""
        ).strip(),
        "account_name": (
            os.environ.get("BANK_ACCOUNT_NAME")
            or getattr(settings, "BANK_ACCOUNT_NAME", "")
            or ""
        ).strip(),
        "account_number": (
            os.environ.get("BANK_ACCOUNT_NUMBER")
            or getattr(settings, "BANK_ACCOUNT_NUMBER", "")
            or ""
        ).strip(),
        "routing_number": (
            os.environ.get("BANK_ROUTING_NUMBER")
            or getattr(settings, "BANK_ROUTING_NUMBER", "")
            or ""
        ).strip(),
    }


@login_required
@require_GET
def stripe_create_session(request, order_id):
    order = get_object_or_404(Order, pk=order_id, ordered_by_id=request.user.id)
    if str(order.payment_method) != "stripe":
        messages.error(request, "This order is not using Stripe payment.")
        return redirect("shop-order-success", order_id=order.id)
    secret_key = _stripe_secret_key()
    if not secret_key:
        messages.error(request, "Stripe is not configured. Contact an administrator.")
        return redirect("shop-checkout")
    try:
        import stripe
    except Exception:
        messages.error(
            request, "Stripe SDK is not installed. Contact an administrator."
        )
        return redirect("shop-checkout")

    stripe.api_key = secret_key
    success_url = (
        request.build_absolute_uri(reverse("stripe-success"))
        + f"?session_id={{CHECKOUT_SESSION_ID}}&order_id={order.id}"
    )
    cancel_url = (
        request.build_absolute_uri(reverse("stripe-cancel")) + f"?order_id={order.id}"
    )

    amount = int(round(float(order.total_revenue or 0.0) * 100))
    if amount <= 0:
        messages.error(request, "Invalid order amount; cannot start payment.")
        return redirect("shop-checkout")

    session = stripe.checkout.Session.create(
        mode="payment",
        success_url=success_url,
        cancel_url=cancel_url,
        payment_method_types=["card"],
        line_items=[
            {
                "price_data": {
                    "currency": "cny",
                    "product_data": {"name": f"Order #{order.id}"},
                    "unit_amount": amount,
                },
                "quantity": 1,
            }
        ],
        metadata={"order_id": str(order.id), "user_id": str(request.user.id)},
    )
    Order.objects.filter(pk=order.id).update(
        stripe_session_id=session.id, payment_status=Order.PaymentStatus.UNPAID
    )
    return redirect(session.url, permanent=False)


@login_required
@require_GET
def stripe_success(request):
    order_id = request.GET.get("order_id")
    session_id = (request.GET.get("session_id") or "").strip()
    if not order_id or not str(order_id).isdigit():
        return HttpResponseBadRequest("Missing order id")
    order = get_object_or_404(Order, pk=int(order_id), ordered_by_id=request.user.id)

    secret_key = _stripe_secret_key()
    if not secret_key:
        messages.error(request, "Stripe is not configured. Contact an administrator.")
        return redirect("shop-checkout")
    try:
        import stripe
    except Exception:
        messages.error(
            request, "Stripe SDK is not installed. Contact an administrator."
        )
        return redirect("shop-checkout")
    stripe.api_key = secret_key

    if not session_id:
        session_id = order.stripe_session_id or ""
    if not session_id:
        messages.error(request, "Could not start payment session. Try again.")
        return redirect("shop-checkout")

    session = stripe.checkout.Session.retrieve(session_id)
    if getattr(session, "payment_status", "") == "paid":
        with transaction.atomic():
            o = Order.objects.select_for_update().get(pk=order.id)
            o.payment_method = "stripe"
            o.payment_status = Order.PaymentStatus.PAID
            o.paid_at = timezone.now()
            o.status = Order.Status.PAID
            if o.workflow_status == Order.WorkflowStatus.PENDING_CONFIRM:
                o.workflow_status = Order.WorkflowStatus.CONFIRMED
            o.save(
                update_fields=[
                    "payment_method",
                    "payment_status",
                    "paid_at",
                    "status",
                    "workflow_status",
                ]
            )
            reverse_credit_debt_if_counted(o)
        messages.success(request, "Stripe payment successful.")
        return redirect("shop-order-success", order_id=order.id)

    Order.objects.filter(pk=order.id).update(
        payment_status=Order.PaymentStatus.CANCELLED
    )
    messages.error(request, "Payment not completed. Try again.")
    return redirect("shop-checkout")


@login_required
@require_GET
def stripe_cancel(request):
    order_id = request.GET.get("order_id")
    if order_id and str(order_id).isdigit():
        Order.objects.filter(pk=int(order_id), ordered_by_id=request.user.id).update(
            payment_status=Order.PaymentStatus.UNPAID
        )
    messages.warning(request, "Payment cancelled. You can start again.")
    return redirect("shop-checkout")


@login_required
@require_GET
def bank_transfer_instructions(request, order_id):
    order = get_object_or_404(Order, pk=order_id, ordered_by_id=request.user.id)
    bank = getattr(settings, "BANK_TRANSFER_INFO", None) or {}
    return render(
        request,
        "shop/bank_transfer_instructions.html",
        {"order": order, "bank_info": bank},
    )


@login_required
@require_POST
def update_transfer_reference(request, order_id):
    order = get_object_or_404(Order, pk=order_id, ordered_by_id=request.user.id)
    ref = (request.POST.get("transfer_reference") or "").strip()[:255]
    order.transfer_reference = ref
    if (
        order.payment_method == Order.PaymentMethod.BANK_TRANSFER
        and order.payment_status == Order.PaymentStatus.UNPAID
    ):
        order.payment_status = Order.PaymentStatus.PENDING_CONFIRMATION
        order.save(update_fields=["transfer_reference", "payment_status"])
    else:
        order.save(update_fields=["transfer_reference"])
    messages.success(request, "Transfer reference saved.")
    return redirect("my-orders")


@login_required
@internal_user_required
def mark_order_payment_failed(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    order.payment_status = Order.PaymentStatus.CANCELLED
    order.save(update_fields=["payment_status"])
    messages.success(request, "Marked as cancelled.")
    return redirect("orders-list")


PRODUCT_CSV_HEADER = [
    "category",
    "name",
    "sku",
    "unit_label",
    "case_label",
    "price_single",
    "price_case",
    "shelf_life_months",
    "can_split_sale",
    "minimum_order_qty",
    "is_active",
]


def _csv_parse_bool(val):
    if val is None:
        return False
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    raise ValueError(f"Cannot parse boolean: {val!r}")


def _csv_parse_float(val):
    if val is None or str(val).strip() == "":
        return 0.0
    return float(str(val).strip())


def _csv_parse_int(val):
    if val is None or str(val).strip() == "":
        return 0
    return int(float(str(val).strip()))


def _csv_pad_row(row, n):
    row = list(row)
    while len(row) < n:
        row.append("")
    return row[:n]


def product_csv_import(request):
    """后台商品 CSV 批量导入（由 admin_view 挂载，仅 staff）。"""
    if request.method == "GET":
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "Product CSV import",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
            },
        )

    upload = request.FILES.get("csv_file")
    if not upload:
        messages.error(request, "Please choose a CSV file to upload.")
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "Product CSV import",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": "No file selected.",
            },
        )

    try:
        raw = upload.read()
        if isinstance(raw, bytes):
            text = raw.decode("utf-8-sig")
        else:
            text = raw
    except UnicodeDecodeError:
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "Product CSV import",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": "File is not valid UTF-8. Save the CSV as UTF-8.",
            },
        )

    reader = csv.reader(io.StringIO(text))
    try:
        header_row = next(reader)
    except StopIteration:
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "Product CSV import",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": "CSV is empty.",
            },
        )

    header = [h.strip() for h in header_row]
    if header != PRODUCT_CSV_HEADER:
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "Product CSV import",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": f"Header must match exactly (including order). Expected: {','.join(PRODUCT_CSV_HEADER)}",
            },
        )

    created = 0
    updated = 0
    failed = 0
    failures = []

    line_no = 1
    for raw_row in reader:
        line_no += 1
        raw_row = _csv_pad_row(raw_row, len(PRODUCT_CSV_HEADER))
        if not any(str(c).strip() for c in raw_row):
            continue

        row = dict(zip(PRODUCT_CSV_HEADER, raw_row))

        try:
            cat_name = str(row["category"]).strip()
            name = str(row["name"]).strip()
            sku = str(row["sku"]).strip()
            if not cat_name or not name or not sku:
                raise ValueError("category, name, and sku are required")

            unit_label = str(row.get("unit_label") or "").strip()
            case_label = str(row.get("case_label") or "").strip()
            price_single = _csv_parse_float(row.get("price_single"))
            price_case = _csv_parse_float(row.get("price_case"))
            shelf_life_months = _csv_parse_int(row.get("shelf_life_months"))
            if shelf_life_months < 0:
                raise ValueError("shelf_life_months is invalid")
            can_split_sale = _csv_parse_bool(row.get("can_split_sale"))
            minimum_order_qty = _csv_parse_float(row.get("minimum_order_qty"))
            if minimum_order_qty <= 0:
                raise ValueError("minimum_order_qty must be greater than 0")
            is_active = _csv_parse_bool(row.get("is_active"))

            with transaction.atomic():
                category, _ = ProductCategory.objects.get_or_create(
                    name=cat_name,
                    defaults={"sort_order": 0, "is_active": True},
                )
                product = Product.objects.filter(sku=sku).first()
                fields = {
                    "category": category,
                    "name": name,
                    "unit_label": unit_label,
                    "case_label": case_label,
                    "price_single": float(price_single),
                    "price_case": float(price_case),
                    "shelf_life_months": shelf_life_months,
                    "can_split_sale": can_split_sale,
                    "minimum_order_qty": float(minimum_order_qty),
                    "is_active": is_active,
                }
                if product:
                    for k, v in fields.items():
                        setattr(product, k, v)
                    product.save()
                    updated += 1
                else:
                    Product.objects.create(
                        sku=sku,
                        **fields,
                    )
                    created += 1
        except Exception as exc:
            failed += 1
            failures.append((line_no, str(exc)))

    return render(
        request,
        "admin/tea_supply/product/import_csv.html",
        {
            "title": "Product CSV import result",
            "header_line": ",".join(PRODUCT_CSV_HEADER),
            "import_result": {
                "created": created,
                "updated": updated,
                "failed": failed,
                "failures": failures,
            },
        },
    )


@login_required(login_url="/login/")
@require_http_methods(["GET", "POST"])
def driver_orders(request):
    """Driver V1: orders where assigned_driver is the current user."""
    if request.method == "POST":
        order_id = (request.POST.get("order_id") or "").strip()
        action = (request.POST.get("action") or "").strip()
        if not order_id or action not in ("start_delivery", "mark_completed"):
            messages.error(request, "Invalid request.")
            return redirect("driver-orders")
        order = get_object_or_404(Order, pk=order_id, assigned_driver_id=request.user.id)
        if action == "start_delivery":
            # V1 state transition guard:
            # assigned -> delivering only.
            if order.delivery_status != "assigned":
                messages.error(
                    request,
                    "Cannot start delivery: order is not in 'Assigned' state.",
                )
                return redirect("driver-orders")
            order.delivery_status = "delivering"
            order.save(update_fields=["delivery_status"])
            messages.success(request, "Delivery status updated to Delivering.")
        else:
            # V1 state transition guard:
            # delivering -> completed only.
            if order.delivery_status != "delivering":
                messages.error(
                    request,
                    "Cannot complete delivery: order is not in 'Delivering' state.",
                )
                return redirect("driver-orders")
            order.delivery_status = "completed"
            order.save(update_fields=["delivery_status"])
            messages.success(request, "Delivery status updated to Completed.")
        return redirect("driver-orders")

    orders = (
        Order.objects.filter(assigned_driver_id=request.user.id)
        .select_related("customer", "assigned_vehicle")
        .order_by("-created_at")
    )
    return render(request, "tea_supply/driver_orders.html", {"orders": orders})
