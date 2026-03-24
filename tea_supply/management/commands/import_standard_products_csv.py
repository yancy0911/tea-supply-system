"""从标准商品 CSV 导入/更新 Product（按 sku upsert，不删除旧数据）。"""

import csv
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from tea_supply.models import Product, ProductCategory

REQUIRED_COLUMNS = {
    "sku",
    "name",
    "category",
    "price_single",
    "price_case",
    "unit",
    "spec",
    "image_url",
}


def _f(v, default=0.0):
    s = str(v or "").strip()
    if not s:
        return float(default)
    return float(s.replace(",", ""))


def _image_to_rel_path(image_url: str) -> str:
    s = str(image_url or "").strip()
    if not s:
        return ""
    if s.startswith("/media/"):
        return s[len("/media/") :].lstrip("/")
    if s.startswith("media/"):
        return s[len("media/") :].lstrip("/")
    # 若给的是完整 URL 或其他格式，这里先原样保留
    return s


class Command(BaseCommand):
    help = "Import/update Product from standard CSV using sku as unique key"

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            default=None,
            help="CSV path (default: <BASE_DIR>/data/products_2026.csv)",
        )

    def handle(self, *args, **opts):
        default_path = Path(settings.BASE_DIR) / "data" / "products_2026.csv"
        path = Path(opts["path"] or default_path)
        if not path.is_file():
            self.stderr.write(self.style.ERROR(f"CSV 不存在: {path}（请先放置 data/products_2026.csv）"))
            return

        created = 0
        updated = 0
        skipped = []

        with path.open("r", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            header = [str(h or "").strip() for h in (reader.fieldnames or [])]
            missing = sorted(REQUIRED_COLUMNS - set(header))
            if missing:
                self.stderr.write(self.style.ERROR(f"CSV 缺少字段: {missing}"))
                return

            for line_no, row in enumerate(reader, start=2):
                if not row or not any(str(v or "").strip() for v in row.values()):
                    continue
                try:
                    sku = str(row.get("sku") or "").strip()
                    name = str(row.get("name") or "").strip()
                    category_name = str(row.get("category") or "").strip() or "默认分类"
                    if not sku or not name:
                        skipped.append((line_no, "sku 或 name 为空"))
                        continue

                    price_single = _f(row.get("price_single"), default=0.0)
                    price_case = _f(row.get("price_case"), default=price_single)
                    unit_label = str(row.get("unit") or "").strip()
                    case_label = str(row.get("spec") or "").strip()
                    image = _image_to_rel_path(str(row.get("image_url") or ""))

                    with transaction.atomic():
                        category, _ = ProductCategory.objects.get_or_create(
                            name=category_name,
                            defaults={"sort_order": 0, "is_active": True},
                        )
                        defaults = {
                            "category": category,
                            "name": name,
                            "unit_label": unit_label,
                            "case_label": case_label,
                            "price_single": price_single,
                            "price_case": price_case,
                            "is_active": True,
                            "current_stock": 100.0,
                            "safety_stock": 10.0,
                            "image": image,
                        }
                        product, is_created = Product.objects.update_or_create(
                            sku=sku,
                            defaults=defaults,
                        )
                        if is_created:
                            created += 1
                        else:
                            updated += 1
                except Exception as exc:
                    skipped.append((line_no, str(exc)))

        self.stdout.write(self.style.SUCCESS("标准商品 CSV 导入完成"))
        self.stdout.write(f"新建: {created}")
        self.stdout.write(f"更新: {updated}")
        self.stdout.write("默认值: is_active=True, current_stock=100, safety_stock=10")
        if skipped:
            self.stdout.write(self.style.WARNING("以下行跳过/失败:"))
            for ln, reason in skipped:
                self.stdout.write(f"  第 {ln} 行: {reason}")
