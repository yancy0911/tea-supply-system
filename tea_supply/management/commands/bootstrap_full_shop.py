"""
一次性：规范化 CSV → 导入分类与商品 → 清洗可售字段 → 尝试按媒体补全图片。
在 Render Shell 或本地执行同一命令即可对「当前 DATABASE」完成商城数据就绪。

用法:
  python manage.py bootstrap_full_shop
  python manage.py bootstrap_full_shop --skip-csv   # 仅清洗+补图（CSV 已导入过）
"""
from __future__ import annotations

import traceback
import importlib.util
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


def _load_write_csv_module():
    path = Path(settings.BASE_DIR) / "data" / "write_products_csv_tmp.py"
    spec = importlib.util.spec_from_file_location("write_products_csv_tmp", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


class Command(BaseCommand):
    help = "端到端：写 CSV → 导入分类/商品 → 清洗 → 补图（当前数据库）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--skip-csv",
            action="store_true",
            help="跳过从 data/products_import_ready.csv 生成 /tmp CSV 与导入",
        )

    def handle(self, *args, **opts):
        errs: list[str] = []

        def _step(name: str, fn):
            self.stdout.write(self.style.NOTICE(f"—— {name} ——"))
            try:
                fn()
            except Exception as e:
                errs.append(f"{name}: {e}\n{traceback.format_exc()}")
                self.stderr.write(self.style.ERROR(f"{name} 失败: {e}"))

        if not opts["skip_csv"]:
            def write_csv():
                wmod = _load_write_csv_module()
                rc = wmod.main()
                if rc != 0:
                    raise RuntimeError(f"write_products_csv_tmp 退出码 {rc}")

            _step("1/5 规范化并写入 /tmp/product_categories.csv 与 /tmp/products.csv", write_csv)

            def imp_cat():
                call_command("import_product_categories_ready", "/tmp/product_categories.csv")

            def imp_prod():
                call_command("import_products_ready", "/tmp/products.csv")

            _step("2/5 导入分类", imp_cat)
            _step("3/5 导入商品", imp_prod)
        else:
            self.stdout.write(self.style.WARNING("已 --skip-csv，跳过 CSV 生成与导入"))

        _step("4/5 清洗商品（价格/单位/库存/上架）", lambda: call_command("clean_products_catalog"))

        def backfill():
            call_command("backfill_product_images")

        _step("5/5 按 MEDIA 目录补全图片路径", backfill)

        if errs:
            self.stderr.write(self.style.WARNING(f"共 {len(errs)} 步异常（已尽力继续）"))
            for e in errs:
                self.stderr.write(e[:2000])
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "bootstrap_full_shop 完成：商城数据已就绪。请访问 /shop/ 验证浏览、购物车与下单。"
                )
            )
