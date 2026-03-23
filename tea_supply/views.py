import csv
import io
import json
import hashlib
import secrets
import time
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Min, Q, Sum
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from .models import (
    CUSTOMER_TIER_DISCOUNT,
    Customer,
    CustomerProductPrice,
    Ingredient,
    Order,
    OrderItem,
    Product,
    ProductCategory,
    _stock_need_for_line,
    resolve_selling_unit_price,
)


def _unsettled_order_statuses():
    return (Order.Status.PENDING,)


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


SHOP_SESSION_CUSTOMER_KEY = "shop_customer_id"


def _make_order_submit_signature(*, prefix: str, customer_id: str, lines_json: str, extra=None):
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


def _check_and_set_submit_lock(*, request, lock_key: str, signature: str, window_seconds: int = 5) -> bool:
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


def submit_order_from_lines(request, customer_obj, lines, *, from_shop=False, shipping=None, guest_session_key=None):
    """
    从购物车明细创建订单（批发录单 / 客户商城共用逻辑）。
    request: 可为 None（店员录单）；商城下单传入 request 以绑定 session。
    lines: 已解析的 list，元素为 dict：product_id, sale_type, quantity
    from_shop=True 时跳过赊账/额度校验（商城现结/线下对账由老板处理）。
    """
    try:
        if not isinstance(lines, list) or len(lines) == 0:
            raise ValidationError("请至少添加一条订单明细后再提交")

        shipping = shipping or {}
        if from_shop:
            cn = (shipping.get("contact_name") or "").strip()
            phone = (shipping.get("delivery_phone") or "").strip()
            addr = (shipping.get("delivery_address") or "").strip()
            if not cn:
                raise ValidationError("请填写收货人")
            if not phone:
                raise ValidationError("请填写联系电话")
            if not addr:
                raise ValidationError("请填写配送地址")
            if customer_obj is not None:
                reason = customer_obj.shop_order_denial_reason()
                if reason:
                    raise ValidationError(reason)
        else:
            if customer_obj is None:
                raise ValidationError("请选择客户")
            if not customer_obj.allow_credit:
                raise ValidationError("该客户不允许赊账下单（原材料供应链未开放欠款权限）")

        current_unsettled = unsettled_amount_for_customer(customer_obj) if customer_obj else 0.0
        credit_limit = float(customer_obj.credit_limit) if customer_obj else 0.0

        order_total = 0.0
        validated = []
        for raw in lines:
            if raw.get("product_id") is None:
                raise ValidationError("订单明细缺少商品")
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
                raise ValidationError("无效的销售方式")

            p = Product.objects.get(pk=pid)
            if not p.is_active:
                raise ValidationError(f"商品「{p.name}」已停用")
            if sale_type == OrderItem.SaleType.SINGLE and not p.can_split_sale:
                raise ValidationError(f"商品「{p.name}」不可拆卖，请选择整箱")
            if qty < float(p.minimum_order_qty):
                raise ValidationError(f"「{p.name}」数量不能低于起订量 {p.minimum_order_qty}")

            if customer_obj is None:
                unit_price = float(p.price_case) if sale_type == OrderItem.SaleType.CASE else float(p.price_single)
            else:
                unit_price, _ = resolve_selling_unit_price(customer_obj, p, sale_type)
            if float(unit_price) <= 0:
                raise ValidationError(f"商品「{p.name}」无有效价格（请询价），无法下单")
            line_amt = qty * float(unit_price)
            order_total += line_amt
            validated.append({"product_id": pid, "sale_type": sale_type, "quantity": qty})

        needs = defaultdict(float)
        for v in validated:
            p = Product.objects.get(pk=v["product_id"])
            probe = OrderItem(product=p, quantity=v["quantity"], sale_type=v["sale_type"])
            needs[v["product_id"]] += float(_stock_need_for_line(probe, p))
        for pid, need in needs.items():
            if need <= 0:
                continue
            p = Product.objects.get(pk=pid)
            if p.ingredient_id:
                ing = Ingredient.objects.filter(pk=p.ingredient_id).first()
                cur = float(ing.stock) if ing else 0.0
                if cur < need:
                    raise ValidationError(
                        f"商品「{p.name}」库存不足：本单共需 {need:g}，当前可售 {cur:g}，请减少数量"
                    )
            else:
                cur = float(p.stock_quantity or 0)
                if cur < need:
                    raise ValidationError(
                        f"商品「{p.name}」库存不足：本单共需 {need:g}，当前可售 {cur:g}，请减少数量"
                    )

        if not from_shop and credit_limit > 0:
            if current_unsettled >= credit_limit:
                raise ValidationError("当前应收账款已用满信用额度，无法继续下单")
            if current_unsettled + order_total > credit_limit:
                raise ValidationError("本次下单将超出信用额度，无法继续下单")

        with transaction.atomic():
            create_kwargs = {"confirmed": False}
            if customer_obj is not None:
                create_kwargs["customer"] = customer_obj
            if from_shop:
                ts = timezone.now().strftime("%m%d%H%M")
                if customer_obj is not None:
                    create_kwargs["name"] = f"商城-{customer_obj.name}-{ts}"
                    create_kwargs["guest_session_key"] = ""
                else:
                    create_kwargs["name"] = f"商城-游客-{ts}"
                    create_kwargs["guest_session_key"] = _guest_order_session_key(request, guest_session_key)
                create_kwargs["workflow_status"] = Order.WorkflowStatus.PENDING_CONFIRM
                create_kwargs["contact_name"] = (shipping.get("contact_name") or "")[:100]
                create_kwargs["delivery_phone"] = (shipping.get("delivery_phone") or "")[:30]
                create_kwargs["store_name"] = (shipping.get("store_name") or "")[:200]
                create_kwargs["delivery_address"] = (shipping.get("delivery_address") or "")[:500]
                create_kwargs["order_note"] = (shipping.get("order_note") or "")[:2000]
            else:
                create_kwargs["customer"] = customer_obj
            order = Order.objects.create(**create_kwargs)
            for v in validated:
                OrderItem.objects.create(
                    order=order,
                    product_id=v["product_id"],
                    sale_type=v["sale_type"],
                    quantity=v["quantity"],
                )
        return order
    except Exception as e:
        print(e)
        raise


def get_shop_customer(request):
    cid = request.session.get(SHOP_SESSION_CUSTOMER_KEY)
    if not cid:
        return None
    return Customer.objects.filter(pk=cid).first()


def shop_order_permission(customer):
    """
    商城是否允许提交订单。
    返回 (can_order: bool, block_hint: str)；block_hint 在不可下单时用于按钮提示/弹窗。
    未登录：允许以游客身份下单（基础价）；已登录：按账号状态。
    """
    if not customer:
        return True, ""
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
    img = (getattr(p, "image", None) or "").strip()
    if img:
        image_url = "/media/" + img.lstrip("/")
    else:
        image_url = _default_product_image_url()
    base_s = float(p.price_single)
    base_c = float(p.price_case)
    ds, note_s = resolve_selling_unit_price(customer, p, OrderItem.SaleType.SINGLE)
    dc, note_c = resolve_selling_unit_price(customer, p, OrderItem.SaleType.CASE)
    stock_disp = None
    if p.ingredient_id:
        try:
            ing = p.ingredient
            stock_disp = float(ing.stock)
        except Exception:
            stock_disp = None
    else:
        stock_disp = float(p.stock_quantity)
    row = {
        "id": p.id,
        "category_id": int(p.category_id) if p.category_id else None,
        "category_name": p.category.name if p.category_id else "",
        "name": p.name,
        "sku": p.sku,
        "unit_label": (p.unit_label or "").strip() or "per unit",
        "case_label": (p.case_label or "").strip() or "per case",
        "price_single": base_s,
        "price_case": base_c,
        "base_single": base_s,
        "base_case": base_c,
        "display_single": float(ds),
        "display_case": float(dc),
        "strike_single": abs(float(ds) - base_s) > 1e-6,
        "strike_case": abs(float(dc) - base_c) > 1e-6,
        "price_note": (f"单品:{note_s} · 整箱:{note_c}" if note_s != note_c else note_s),
        "price_note_single": note_s,
        "price_note_case": note_c,
        "can_split_sale": p.can_split_sale,
        "minimum_order_qty": float(p.minimum_order_qty),
        "image_url": image_url,
        "has_image": True,
        "price_on_request": base_s <= 0 and base_c <= 0,
        "can_quote_single": base_s > 0,
        "can_quote_case": base_c > 0,
        "stock_quantity": stock_disp,
        "uses_ingredient_stock": bool(p.ingredient_id),
        "units_per_case": float(p.units_per_case),
    }
    return row


def _dunning_time_and_supply(amount, credit_limit, days_since_earliest):
    """超额度优先；否则按最早待处理订单未结算天数分档。"""
    amt = float(amount)
    cl = float(credit_limit)
    no_limit = cl <= 0
    if cl > 0 and amt > cl:
        return {
            "dunning_status": "高风险",
            "supply_advice": "暂停供货",
            "style": "high_risk",
            "no_limit": False,
        }
    if amt <= 0:
        return {
            "dunning_status": "正常",
            "supply_advice": "可继续供货",
            "style": "normal",
            "no_limit": no_limit,
        }
    d = int(days_since_earliest)
    if d < 3:
        return {
            "dunning_status": "正常",
            "supply_advice": "现结交易" if no_limit else "可继续供货",
            "style": "normal",
            "no_limit": no_limit,
        }
    if d <= 7:
        return {
            "dunning_status": "提醒",
            "supply_advice": "提醒对账",
            "style": "watch",
            "no_limit": no_limit,
        }
    if d <= 15:
        return {
            "dunning_status": "催款",
            "supply_advice": "优先催款",
            "style": "dunning",
            "no_limit": no_limit,
        }
    return {
        "dunning_status": "严重催款",
        "supply_advice": "立即催收并评估停供",
        "style": "severe",
        "no_limit": no_limit,
    }


@login_required
def wholesale_order_entry(request):
    customers = Customer.objects.all().order_by("name")
    categories = ProductCategory.objects.filter(is_active=True).order_by("sort_order", "id")
    products = Product.objects.filter(is_active=True).select_related("category").order_by("category", "name")
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
            if _check_and_set_submit_lock(request=request, lock_key=lock_key, signature=sig):
                messages.error(request, "检测到重复提交，请不要重复点击“录单成功”。")
                return redirect("wholesale-order-entry")
            lock_set = True

            lines = json.loads(lines_raw)
            customer_obj = Customer.objects.get(pk=customer_id)
            submit_order_from_lines(request, customer_obj, lines)
            _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
            messages.success(request, "录单成功")
            return redirect("wholesale-order-entry")
        except Customer.DoesNotExist as exc:
            print(exc)
            messages.error(request, "客户或商品不存在，请刷新重试")
        except Product.DoesNotExist as exc:
            print(exc)
            messages.error(request, "客户或商品不存在，请刷新重试")
        except (ValueError, TypeError, ValidationError) as exc:
            print(exc)
            messages.error(request, str(exc))
        except json.JSONDecodeError as exc:
            print(exc)
            messages.error(request, "订单明细格式错误")
        finally:
            # 失败也清锁，允许用户修正后重试
            # （只有签名一致才会清除，避免误删其他请求的锁）
            try:
                sig = locals().get("sig")
                lock_key = locals().get("lock_key")
                lock_set = locals().get("lock_set", False)
                if sig and lock_key and lock_set:
                    _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
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
        "tier_discounts_json": json.dumps({k: float(v) for k, v in CUSTOMER_TIER_DISCOUNT.items()}, ensure_ascii=False),
        "exclusive_prices_json": json.dumps(exclusive_map, ensure_ascii=False),
        "today_order_count": Order.objects.count(),
    }
    return render(request, "wholesale_order_form.html", context)


def shop_home(request):
    """客户前台商城（/shop/）：商品与分类均来自数据库；定价随 session 客户身份变化。"""
    categories = ProductCategory.objects.filter(is_active=True).order_by("sort_order", "id")
    customer = get_shop_customer(request)
    products = (
        Product.objects.filter(is_active=True)
        .select_related("category", "ingredient")
        .order_by("category__sort_order", "category_id", "name")
    )
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
        if not row["has_image"]:
            missing_images.append({"sku": p.sku, "name": p.name})
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
        "missing_images": missing_images,
        "missing_prices": missing_prices,
    }
    return render(request, "shop/shop_home.html", ctx)


@require_POST
def shop_login(request):
    phone = (request.POST.get("phone") or "").strip()
    if not phone:
        messages.error(request, "请输入手机号")
        return redirect("shop-home")
    c = Customer.objects.filter(phone=phone).first()
    if not c:
        c = Customer.objects.create(
            name=f"新客户-{phone}",
            phone=phone,
            address="（待完善）",
            delivery_zone="（待分配）",
            account_status=Customer.AccountStatus.PENDING,
        )
        request.session[SHOP_SESSION_CUSTOMER_KEY] = c.pk
        request.session.modified = True
        messages.info(
            request,
            "已用手机号创建账号，当前为「待审核」。审核通过后即可下单；可先浏览商品。",
        )
        messages.warning(request, "账号审核中，请联系店家开通采购权限")
        return redirect("shop-home")
    request.session[SHOP_SESSION_CUSTOMER_KEY] = c.pk
    request.session.modified = True
    if c.account_status == Customer.AccountStatus.PENDING:
        messages.warning(request, "账号审核中，请联系店家开通采购权限")
    elif c.account_status == Customer.AccountStatus.DISABLED:
        messages.warning(request, "账号已禁用，无法下单。可浏览商品；如有疑问请联系店家。")
    else:
        messages.success(request, f"欢迎，{c.name}")
    return redirect("shop-home")


@require_GET
def shop_logout(request):
    request.session.pop(SHOP_SESSION_CUSTOMER_KEY, None)
    messages.info(request, "已退出客户登录")
    return redirect("shop-home")


@require_GET
def shop_checkout(request):
    customer = get_shop_customer(request)
    can_order, order_hint = shop_order_permission(customer)
    categories = ProductCategory.objects.filter(is_active=True).order_by("sort_order", "id")
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
        },
    )


@require_GET
def shop_product_detail(request, product_id):
    customer = get_shop_customer(request)
    p = get_object_or_404(Product.objects.select_related("category", "ingredient"), pk=product_id)
    if not p.is_active:
        messages.error(request, "该商品已下架")
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
    order = get_object_or_404(Order.objects.prefetch_related("items__product"), pk=order_id)
    customer = get_shop_customer(request)
    if order.customer_id:
        if not customer or order.customer_id != customer.pk:
            messages.error(request, "无权查看该订单")
            return redirect("shop-home")
    else:
        sk = request.session.session_key or ""
        if not sk or (order.guest_session_key or "") != sk:
            messages.error(request, "无权查看该订单")
            return redirect("shop-home")
    return render(request, "shop/shop_order_success.html", {"order": order})


@require_POST
def shop_submit_order(request):
    if not request.session.session_key:
        request.session.create()
    session_key = request.session.session_key or ""

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
    }
    sig_id = str(customer.pk) if customer else f"guest::{session_key}"
    sig = _make_order_submit_signature(
        prefix="shop",
        customer_id=sig_id,
        lines_json=str(lines_raw),
        extra=submit_extra,
    )
    lock_key = f"submit_lock::shop::{sig_id}"
    if _check_and_set_submit_lock(request=request, lock_key=lock_key, signature=sig):
        messages.error(request, "检测到重复提交，请不要重复点击“确认提交订单”。")
        if (request.POST.get("next") or "").strip() == "checkout":
            return redirect("shop-checkout")
        return redirect("shop-home")

    fail_redirect = (
        "shop-checkout" if (request.POST.get("next") or "").strip() == "checkout" else "shop-home"
    )

    try:
        lines = json.loads(lines_raw)
        shipping = {
            "contact_name": request.POST.get("contact_name", ""),
            "delivery_phone": request.POST.get("delivery_phone", ""),
            "store_name": request.POST.get("store_name", ""),
            "delivery_address": request.POST.get("delivery_address", ""),
            "order_note": request.POST.get("order_note", ""),
        }
        if customer:
            order = submit_order_from_lines(
                request, customer, lines, from_shop=True, shipping=shipping, guest_session_key=None
            )
        else:
            order = submit_order_from_lines(
                request, None, lines, from_shop=True, shipping=shipping, guest_session_key=session_key or None
            )
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.success(request, "批发订单已提交")
        return redirect("shop-order-success", order_id=order.id)
    except (ValidationError, ValueError, TypeError) as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, str(exc))
    except json.JSONDecodeError as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, "订单数据格式错误")
    except Product.DoesNotExist as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, "商品不存在，请刷新后重试")
    except Exception as exc:
        print(exc)
        _clear_submit_lock_if_matches(request=request, lock_key=lock_key, signature=sig)
        messages.error(request, f"提交失败：{exc}")
    return redirect(fail_redirect)


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
            "label": "观察中",
            "action": "暂无销量，继续观察",
        }

    is_red = False
    if days_cover is not None and days_cover <= 7:
        is_red = True
    if stock < 0.25 * sold_30d:
        is_red = True

    if is_red:
        return {"level": "red", "label": "红色预警", "action": "立即补货（重要）"}

    is_yellow = False
    if days_cover is not None and days_cover <= 15:
        is_yellow = True
    if stock < 0.5 * sold_30d:
        is_yellow = True

    if is_yellow:
        return {"level": "yellow", "label": "黄色关注", "action": "尽快补货"}

    return {"level": "green", "label": "库存安全", "action": "库存安全"}


@login_required
def replenishment_dashboard(request):
    """
    老板决策版补货预警：风险分级、排序、汇总、建议采购量（60 天需求 − 当前库存）。
    """
    sold_7_map = _sales_units_by_product(7)
    sold_30_map = _sales_units_by_product(30)

    products = Product.objects.select_related("category", "ingredient").order_by("category", "name")
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
            sales_note = "暂无销量参考"
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
        rep_banner = {"kind": "danger", "text": "⚠️ 有商品库存紧张，请优先处理"}
    elif stats["yellow"] > 0:
        rep_banner = {"kind": "warn", "text": "有商品建议尽快补货，请关注黄色预警"}
    else:
        rep_banner = {"kind": "safe", "text": "当前库存整体安全"}

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
def inventory_list(request):
    from .models import Ingredient

    ingredients = Ingredient.objects.order_by("name")
    rows = []
    low_count = 0
    for ing in ingredients:
        st = float(ing.stock)
        wl = float(ing.warning_level)
        is_low = st < wl
        if is_low:
            low_count += 1
        suggest = max(0.0, wl - st)
        rows.append(
            {
                "ingredient": ing,
                "low_stock": is_low,
                "status_label": "低库存" if is_low else "正常",
                "suggest_qty": suggest,
            }
        )
    total_count = len(rows)
    normal_count = total_count - low_count
    stats = {
        "total": total_count,
        "low": low_count,
        "normal": normal_count,
    }
    return render(request, "inventory.html", {"rows": rows, "stats": stats})


@login_required
def orders_list(request):
    unsettled_items = OrderItem.objects.filter(order__status__in=_unsettled_order_statuses()).select_related(
        "order__customer", "product"
    )

    amount_by_customer_id = defaultdict(float)
    for item in unsettled_items:
        amount_by_customer_id[item.order.customer_id] += float(item.total_revenue)

    customer_ids = [k for k in amount_by_customer_id.keys() if k is not None]
    customers_map = {c.id: c for c in Customer.objects.filter(pk__in=customer_ids)}

    earliest_pending = Order.objects.filter(status=Order.Status.PENDING).values("customer_id").annotate(
        m=Min("created_at")
    )
    earliest_map = {row["customer_id"]: row["m"] for row in earliest_pending}

    customer_debts = []
    for cid, amount in sorted(amount_by_customer_id.items(), key=lambda x: -x[1]):
        if cid is None:
            name = "未指定客户"
            credit_limit = 0.0
            tier_label = "—"
        else:
            cust = customers_map[cid]
            name = cust.name
            credit_limit = float(cust.credit_limit)
            tier_label = cust.get_customer_level_display()
        days = _days_since_earliest_pending(earliest_map.get(cid))
        ds = _dunning_time_and_supply(amount, credit_limit, days)
        customer_debts.append(
            {
                "name": name,
                "tier": tier_label,
                "amount": amount,
                "credit_limit": credit_limit,
                "no_limit": ds["no_limit"],
                "dunning_status": ds["dunning_status"],
                "supply_advice": ds["supply_advice"],
                "dunning_style": ds["style"],
            }
        )

    orders_qs = Order.objects.select_related("customer").prefetch_related("items__product").order_by("-created_at")
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
        "revenue": float(today_agg["tr"] or 0),
        "cost": float(today_agg["tc"] or 0),
        "profit": float(today_agg["tp"] or 0),
    }

    order_rows = []
    for order in orders:
        items = list(order.items.select_related("product").all())
        if not items:
            order_rows.append(
                {
                    "order": order,
                    "product_name": "-",
                    "sale_type_label": "-",
                    "unit_price": None,
                    "pricing_note": "",
                    "quantity": "-",
                    "line_amount": 0.0,
                    "amount": 0.0,
                    "line_cost": order.total_cost,
                    "line_profit": order.profit,
                }
            )
            continue

        for item in items:
            st = "整箱" if item.sale_type == OrderItem.SaleType.CASE else "单品"
            order_rows.append(
                {
                    "order": order,
                    "product_name": item.product.name,
                    "sale_type_label": st,
                    "unit_price": float(item.unit_price),
                    "pricing_note": item.pricing_note or "—",
                    "quantity": item.quantity,
                    "line_amount": float(item.total_revenue),
                    "amount": float(order.total_revenue),
                    "line_cost": float(order.total_cost),
                    "line_profit": float(order.profit),
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


_MSG_SETTLED_RELEASE = (
    "结算成功：本笔应收账款已核销，该笔不再占用信用额度；"
    "客户欠款与风险提示已实时更新，若此前因额度用满无法录单，现可继续下单。"
)


@login_required
def mark_order_paid(request, order_id):
    order = get_object_or_404(Order, pk=order_id)
    if order.status == Order.Status.PAID:
        messages.info(request, "该订单已是「已结算」，未重复变更额度占用。")
        return redirect("orders-list")
    order.status = Order.Status.PAID
    order.save(update_fields=["status"])
    messages.success(request, _MSG_SETTLED_RELEASE)
    return redirect("orders-list")


@login_required
def order_status_update(request, order_id):
    if request.method == "POST":
        status = request.POST.get("status")
        workflow = request.POST.get("workflow_status")
        valid_statuses = {choice[0] for choice in Order.Status.choices}
        valid_wf = {choice[0] for choice in Order.WorkflowStatus.choices}
        if status in valid_statuses and workflow in valid_wf:
            try:
                with transaction.atomic():
                    order = get_object_or_404(
                        Order.objects.select_related("customer").select_for_update().prefetch_related(
                            "items__product"
                        ),
                        pk=order_id,
                    )
                    old_status = order.status
                    order.status = status
                    order.workflow_status = workflow
                    order.save(update_fields=["status", "workflow_status"])
                    if status == Order.Status.PAID and old_status != Order.Status.PAID:
                        messages.success(request, _MSG_SETTLED_RELEASE)
                    else:
                        messages.success(request, "订单已保存")
                return redirect("orders-list")
            except ValidationError as exc:
                messages.error(request, f"无法保存订单流转：{exc}")
                return redirect("orders-list")
        messages.error(request, "无效的订单状态或履约状态")

    order = get_object_or_404(
        Order.objects.select_related("customer").prefetch_related("items__product"),
        pk=order_id,
    )
    context = {
        "order": order,
        "status_choices": Order.Status.choices,
        "workflow_choices": Order.WorkflowStatus.choices,
    }
    return render(request, "order_status_update.html", context)


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
    raise ValueError(f"无法解析布尔值: {val!r}")


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
                "title": "商品 CSV 导入",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
            },
        )

    upload = request.FILES.get("csv_file")
    if not upload:
        messages.error(request, "请选择要上传的 CSV 文件")
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "商品 CSV 导入",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": "未选择文件",
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
                "title": "商品 CSV 导入",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": "文件无法按 UTF-8 解码，请保存为 UTF-8 编码",
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
                "title": "商品 CSV 导入",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": "CSV 为空",
            },
        )

    header = [h.strip() for h in header_row]
    if header != PRODUCT_CSV_HEADER:
        return render(
            request,
            "admin/tea_supply/product/import_csv.html",
            {
                "title": "商品 CSV 导入",
                "header_line": ",".join(PRODUCT_CSV_HEADER),
                "import_error": f"表头必须完全一致（含顺序）。期望：{','.join(PRODUCT_CSV_HEADER)}",
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
                raise ValueError("category、name、sku 不能为空")

            unit_label = str(row.get("unit_label") or "").strip()
            case_label = str(row.get("case_label") or "").strip()
            price_single = _csv_parse_float(row.get("price_single"))
            price_case = _csv_parse_float(row.get("price_case"))
            shelf_life_months = _csv_parse_int(row.get("shelf_life_months"))
            if shelf_life_months < 0:
                raise ValueError("shelf_life_months 无效")
            can_split_sale = _csv_parse_bool(row.get("can_split_sale"))
            minimum_order_qty = _csv_parse_float(row.get("minimum_order_qty"))
            if minimum_order_qty <= 0:
                raise ValueError("minimum_order_qty 必须大于 0")
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
            "title": "商品 CSV 导入结果",
            "header_line": ",".join(PRODUCT_CSV_HEADER),
            "import_result": {
                "created": created,
                "updated": updated,
                "failed": failed,
                "failures": failures,
            },
        },
    )
