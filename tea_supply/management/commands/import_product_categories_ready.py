"""从 CSV 导入/更新 ProductCategory（与后台 ProductCategoryResource 列一致）。"""
import csv
import io
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from tea_supply.models import ProductCategory

HEADER = ["name", "sort_order", "is_active"]


def _bool(v):
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n", ""):
        return False
    raise ValueError(v)


def _i(v):
    if v is None or str(v).strip() == "":
        return 0
    return int(float(str(v).strip()))


class Command(BaseCommand):
    help = "Import ProductCategory from CSV (name, sort_order, is_active)"

    def add_arguments(self, parser):
        parser.add_argument(
            "csv_path",
            nargs="?",
            default=None,
            help="CSV 路径（可选；与 --path 二选一）",
        )
        parser.add_argument(
            "--path",
            dest="path_opt",
            default=None,
            help="CSV path (default: <BASE_DIR>/data/product_categories_ready.csv)",
        )

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        path = opts["csv_path"] or opts["path_opt"] or str(
            base / "data" / "product_categories_ready.csv"
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
                name = str(row["name"]).strip()
                if not name:
                    skipped.append((line_no, "name 为空"))
                    continue
                sort_order = _i(row.get("sort_order"))
                is_active = _bool(row.get("is_active"))

                with transaction.atomic():
                    obj = ProductCategory.objects.filter(name=name).first()
                    if obj:
                        obj.sort_order = sort_order
                        obj.is_active = is_active
                        obj.save()
                        updated_n += 1
                    else:
                        ProductCategory.objects.create(
                            name=name,
                            sort_order=sort_order,
                            is_active=is_active,
                        )
                        created_n += 1
            except Exception as e:
                skipped.append((line_no, str(e)))

        self.stdout.write(self.style.SUCCESS("—— 分类导入结束 ——"))
        self.stdout.write(f"新建分类: {created_n}")
        self.stdout.write(f"更新分类: {updated_n}")
        if skipped:
            self.stdout.write(self.style.WARNING("未导入或失败行:"))
            for ln, reason in skipped:
                self.stdout.write(f"  第{ln}行: {reason}")
