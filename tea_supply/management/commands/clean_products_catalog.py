"""
清洗 Product：分类占位、名称与 SKU、单位文案、库存、价格与上架状态。
无图时在视图层使用 static/images/default.png，不在库中写假路径。
用法: python manage.py clean_products_catalog
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Count

from tea_supply.models import Product, ProductCategory

DEFAULT_CAT_NAME = "Default"
EMPTY_UNIT = "per unit"
EMPTY_CASE = "per case"


def _clean_name(s: str) -> str:
    t = re.sub(r"\s+", " ", (s or "").strip())
    t = t.replace("\ufffd", "").replace("\u200b", "")
    return (t[:200] if t else "未命名商品")


def _norm_label(s: str, fallback: str) -> str:
    raw = (s or "").strip()
    if not raw:
        return fallback
    t = re.sub(r"\s+", " ", raw.replace("\ufffd", ""))
    t = re.sub(r"\s*/\s*", " / ", t)
    for a, b in (
        ("BAGS", "bags"),
        ("BAG", "bag"),
        ("CASE", "case"),
        ("LBS", "lbs"),
        ("BOTTLES", "bottles"),
        ("BOTTLE", "bottle"),
        ("CANS", "cans"),
        ("CAN", "can"),
    ):
        t = re.sub(rf"\b{a}\b", b, t, flags=re.I)
    return t[:120]


class Command(BaseCommand):
    help = "清洗商品表为可售展示状态（分类/名称/SKU/单位/库存/价格/上架）"

    def handle(self, *args, **options):
        touched: set[int] = set()
        default_cat, _ = ProductCategory.objects.get_or_create(
            name=DEFAULT_CAT_NAME,
            defaults={"sort_order": 9999, "is_active": True},
        )

        # 1) SKU 重复
        dup_sku = (
            Product.objects.values("sku")
            .annotate(n=Count("id"))
            .filter(n__gt=1)
        )
        for row in dup_sku:
            sku_val = row["sku"]
            qs = Product.objects.filter(sku=sku_val).order_by("id")
            first = True
            for p in qs:
                if first:
                    first = False
                    continue
                new_sku = f"{sku_val}-D{p.id}"
                while Product.objects.filter(sku=new_sku).exclude(pk=p.pk).exists():
                    new_sku = f"{new_sku}x"
                p.sku = new_sku[:64]
                p.save(update_fields=["sku"])
                touched.add(p.pk)

        # 2) 同名商品：为后续行追加 (SKU)
        by_name: dict[str, list[Product]] = defaultdict(list)
        for p in Product.objects.all().order_by("id"):
            key = _clean_name(p.name).lower()
            by_name[key].append(p)

        for _key, items in by_name.items():
            if len(items) <= 1:
                continue
            for i, p in enumerate(items):
                base = _clean_name(p.name)
                new_name = base if i == 0 else f"{base} ({p.sku})"[:200]
                if new_name != p.name:
                    p.name = new_name
                    p.save(update_fields=["name"])
                    touched.add(p.pk)

        # 3) 逐条：分类、单位、库存、价格、上架（不删除已有 media 图）
        for p in Product.objects.select_related("category").all():
            changed: list[str] = []
            with transaction.atomic():
                p2 = Product.objects.select_for_update().get(pk=p.pk)

                cat = p2.category
                if cat is not None and not (cat.name or "").strip():
                    p2.category = default_cat
                    changed.append("category")

                nm = _clean_name(p2.name)
                if nm != p2.name:
                    p2.name = nm
                    changed.append("name")

                ul = _norm_label(p2.unit_label, EMPTY_UNIT)
                if ul != p2.unit_label:
                    p2.unit_label = ul
                    changed.append("unit_label")

                cl = _norm_label(p2.case_label, EMPTY_CASE)
                if cl != p2.case_label:
                    p2.case_label = cl
                    changed.append("case_label")

                ps = float(p2.price_single or 0)
                pc = float(p2.price_case or 0)
                if pc == 0 and ps > 0:
                    p2.price_case = ps
                    changed.append("price_case")
                    pc = float(p2.price_case)

                serious_no_price = ps <= 0 and pc <= 0
                want_active = not serious_no_price
                if bool(p2.is_active) != want_active:
                    p2.is_active = want_active
                    changed.append("is_active")

                sq = float(p2.stock_quantity or 0)
                if sq <= 0:
                    p2.stock_quantity = 100.0
                    changed.append("stock_quantity")

                if changed:
                    p2.save()
                    touched.add(p2.pk)

        n = len(touched)
        self.stdout.write(self.style.SUCCESS(f"clean_products_catalog：已清洗 {n} 个商品（有字段被更新）"))
