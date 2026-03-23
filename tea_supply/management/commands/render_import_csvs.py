"""
在 Render 等生产环境 Shell 中执行：先导入分类 CSV，再导入商品 CSV。
使用当前进程的环境变量（含 DATABASE_URL），写入线上数据库。

示例（Render Shell）:
  python manage.py render_import_csvs
  python manage.py render_import_csvs --categories /tmp/product_categories.csv --products /tmp/products.csv
"""
import os
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "生产环境批量导入：先 ProductCategory，再 Product（默认 /tmp 下两个 CSV）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--categories",
            default="/tmp/product_categories.csv",
            help="分类 CSV 路径（默认 /tmp/product_categories.csv）",
        )
        parser.add_argument(
            "--products",
            default="/tmp/products.csv",
            help="商品 CSV 路径（默认 /tmp/products.csv）",
        )

    def handle(self, *args, **opts):
        cpath = Path(opts["categories"]).expanduser()
        ppath = Path(opts["products"]).expanduser()

        if not cpath.is_file():
            self.stderr.write(self.style.ERROR(f"分类文件不存在: {cpath}"))
            return
        if not ppath.is_file():
            self.stderr.write(self.style.ERROR(f"商品文件不存在: {ppath}"))
            return

        db = os.environ.get("DATABASE_URL")
        if not db:
            d0 = settings.DATABASES.get("default", {})
            db = str(d0.get("NAME", ""))
        db_s = str(db)
        if len(db_s) > 96:
            db_s = db_s[:96] + "…"
        self.stdout.write(f"目标数据库（当前环境）: {db_s}")

        self.stdout.write(self.style.NOTICE("1/2 导入分类 …"))
        call_command("import_product_categories_ready", str(cpath))
        self.stdout.write(self.style.NOTICE("2/2 导入商品 …"))
        call_command("import_products_ready", str(ppath))
        self.stdout.write(self.style.SUCCESS("render_import_csvs 全部完成。"))
