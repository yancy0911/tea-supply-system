"""从 data/products_import_ready.csv 导入/更新商品（兼容新旧表头与缺省列）。"""
import csv
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from tea_supply.models import Product, ProductCategory

REQUIRED_COLUMNS = {"category", "name", "sku"}


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
            "--path",
            default=None,
            help="CSV path (default: <BASE_DIR>/data/products_import_ready.csv)",
        )

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        path = opts["path"] or str(base / "data" / "products_import_ready.csv")
        if not os.path.isfile(path):
            self.stderr.write(self.style.ERROR(f"文件不存在: {path}"))
            return

        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            raw_header = reader.fieldnames or []
            header = [str(h or "").strip() for h in raw_header]
            header_set = {h for h in header if h}
            missing_required = sorted(REQUIRED_COLUMNS - header_set)
            if missing_required:
                self.stderr.write(
                    self.style.ERROR(
                        f"表头缺少必需列: {missing_required}；当前表头: {header}"
                    )
                )
                return

            created_cats = set()
            created_n = 0
            updated_n = 0
            skipped = []

            for line_no, row in enumerate(reader, start=2):
                if not row or not any(str(v or "").strip() for v in row.values()):
                    continue
                try:
                    cat_name = str(row.get("category") or "").strip() or "Default"
                    name = str(row.get("name") or "").strip()
                    sku = str(row.get("sku") or "").strip()
                    if not name or not sku:
                        skipped.append((line_no, "name 或 sku 为空"))
                        continue

                    unit_label = str(row.get("unit_label") or "").strip()
                    case_label = str(row.get("case_label") or "").strip()
                    price_single = _f(row.get("price_single"))
                    price_case = _f(row.get("price_case"))
                    if price_case == 0 and price_single != 0:
                        price_case = price_single
                    shelf = _i(row.get("shelf_life_months")) or 12
                    can_split = _bool(row.get("can_split_sale"))
                    min_q = _f(row.get("minimum_order_qty")) or 1.0
                    if min_q <= 0:
                        min_q = 1.0
                    is_active = _bool(row.get("is_active"))
                    image_path = str(row.get("image") or "").strip()
                    # 兼容旧 CSV：缺列或空值时自动补默认值
                    cps_raw = row.get("cost_price_single")
                    cpc_raw = row.get("cost_price_case")
                    sq_raw = row.get("stock_quantity")
                    upc_raw = row.get("units_per_case")
                    cost_price_single = _f(cps_raw) if str(cps_raw or "").strip() else 0.0
                    cost_price_case = _f(cpc_raw) if str(cpc_raw or "").strip() else 0.0
                    stock_quantity = _f(sq_raw) if str(sq_raw or "").strip() else 100.0
                    units_per_case = _f(upc_raw) if str(upc_raw or "").strip() else 1.0
                    if units_per_case <= 0:
                        units_per_case = 1.0
                    por_raw = row.get("price_on_request")
                    if str(por_raw or "").strip() == "":
                        price_on_request = False
                    else:
                        price_on_request = _bool(por_raw)

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
                            "cost_price_single": cost_price_single,
                            "cost_price_case": cost_price_case,
                            "shelf_life_months": max(0, shelf),
                            "can_split_sale": can_split,
                            "minimum_order_qty": min_q,
                            "is_active": is_active,
                            "stock_quantity": stock_quantity,
                            "units_per_case": units_per_case,
                            "image": image_path,
                            "price_on_request": price_on_request,
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
            "已用默认值（若 CSV 缺列或为空）: "
            "cost_price_single→0；cost_price_case→0；stock_quantity→100；units_per_case→1；"
            "shelf_life_months 空→12；minimum_order_qty 空/非法→1；整箱价空→同单品价"
        )
        if skipped:
            self.stdout.write(self.style.WARNING("未导入或失败行:"))
            for ln, reason in skipped:
                self.stdout.write(f"  第{ln}行: {reason}")
