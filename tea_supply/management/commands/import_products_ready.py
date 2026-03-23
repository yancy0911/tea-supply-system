"""从 data/products_import_ready.csv 导入/更新商品（与后台 CSV 规则一致）。"""
import csv
import io
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from tea_supply.models import Product, ProductCategory

HEADER = [
    "category",
    "name",
    "sku",
    "unit_label",
    "case_label",
    "price_single",
    "price_case",
    "cost_price_single",
    "cost_price_case",
    "shelf_life_months",
    "can_split_sale",
    "minimum_order_qty",
    "is_active",
    "stock_quantity",
    "units_per_case",
    "image",
]


def _bool(v):
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    raise ValueError(v)


def _f(v):
    if v is None or str(v).strip() == "":
        return 0.0
    return float(str(v).strip())


def _i(v):
    if v is None or str(v).strip() == "":
        return 0
    return int(float(str(v).strip()))


class Command(BaseCommand):
    help = "Import products from data/products_import_ready.csv"

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default=None,
            help="CSV 文件路径（可选；与 --path 二选一，如 /tmp/products.csv）",
        )
        parser.add_argument(
            "--path",
            dest="path_opt",
            default=None,
            help="CSV path (default: <BASE_DIR>/data/products_import_ready.csv)",
        )

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        path = opts["csv_path"] or opts["path_opt"] or str(
            base / "data" / "products_import_ready.csv"
        )
        if not os.path.isfile(path):
            self.stderr.write(self.style.ERROR(f"文件不存在: {path}"))
            return

        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read()
        reader = csv.reader(io.StringIO(text))
        header = [h.strip() for h in next(reader)]
        if header != HEADER:
            self.stderr.write(self.style.ERROR(f"表头不符，期望: {HEADER}"))
            return

        created_cats = set()
        created_n = 0
        updated_n = 0
        skipped = []

        for line_no, raw in enumerate(reader, start=2):
            raw = list(raw) + [""] * (len(HEADER) - len(raw))
            raw = raw[: len(HEADER)]
            if not any(str(c).strip() for c in raw):
                continue
            row = dict(zip(HEADER, raw))
            try:
                cat_name = str(row["category"]).strip() or "默认分类"
                name = str(row["name"]).strip()
                sku = str(row["sku"]).strip()
                if not name or not sku:
                    skipped.append((line_no, "name 或 sku 为空"))
                    continue

                unit_label = str(row.get("unit_label") or "").strip()
                case_label = str(row.get("case_label") or "").strip()
                price_single = _f(row.get("price_single"))
                price_case = _f(row.get("price_case"))
                # 不自动把整箱价抄成单品价：任一侧为 0 时由 Product.save 标记为询价商品
                cost_single = _f(row.get("cost_price_single"))
                cost_case = _f(row.get("cost_price_case"))
                shelf = _i(row.get("shelf_life_months")) or 12
                can_split = _bool(row.get("can_split_sale"))
                min_q = _f(row.get("minimum_order_qty")) or 1.0
                if min_q <= 0:
                    min_q = 1.0
                is_active = _bool(row.get("is_active"))
                stock_qty = _f(row.get("stock_quantity"))
                units_case = _f(row.get("units_per_case"))
                if units_case <= 0:
                    units_case = 1.0
                image_path = str(row.get("image") or "").strip()

                with transaction.atomic():
                    category, cat_created = ProductCategory.objects.get_or_create(
                        name=cat_name,
                        defaults={"sort_order": 0, "is_active": True},
                    )
                    if cat_created:
                        created_cats.add(cat_name)

                    product = Product.objects.filter(sku=sku).first()
                    fields = {
                        "category": category,
                        "name": name,
                        "unit_label": unit_label,
                        "case_label": case_label,
                        "price_single": price_single,
                        "price_case": price_case,
                        "cost_price_single": cost_single,
                        "cost_price_case": cost_case,
                        "shelf_life_months": max(0, shelf),
                        "can_split_sale": can_split,
                        "minimum_order_qty": min_q,
                        "is_active": is_active,
                        "stock_quantity": stock_qty,
                        "units_per_case": units_case,
                        "image": image_path,
                    }
                    if product:
                        for k, v in fields.items():
                            setattr(product, k, v)
                        product.save()
                        updated_n += 1
                    else:
                        Product.objects.create(sku=sku, **fields)
                        created_n += 1
            except Exception as e:
                skipped.append((line_no, str(e)))

        self.stdout.write(self.style.SUCCESS("—— 导入结束 ——"))
        self.stdout.write(f"新建商品: {created_n}")
        self.stdout.write(f"更新商品: {updated_n}")
        self.stdout.write(f"新建分类数: {len(created_cats)}（{', '.join(sorted(created_cats)) or '无'}）")
        self.stdout.write(
            "已用默认值（若 CSV 未写）: shelf_life_months 空→12；minimum_order_qty 空/非法→1；"
            "单价或整箱价任一侧≤0→询价商品（price_on_request）"
        )
        if skipped:
            self.stdout.write(self.style.WARNING("未导入或失败行:"))
            for ln, reason in skipped:
                self.stdout.write(f"  第{ln}行: {reason}")
