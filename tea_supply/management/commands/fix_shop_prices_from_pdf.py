"""运行 data/extract_mocha_pdf_cards.py：重裁图、生成 CSV 并 import_products_ready。"""
import importlib.util
import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "从 MOCHA PDF 重导商品图与价格（补全 CSV 并导入数据库）"

    def add_arguments(self, parser):
        parser.add_argument("--pdf", default=None, help="PDF 路径（默认与 extract 脚本一致）")

    def handle(self, *args, **opts):
        root = Path(settings.BASE_DIR)
        script = root / "data" / "extract_mocha_pdf_cards.py"
        if not script.is_file():
            self.stderr.write(self.style.ERROR(f"缺少: {script}"))
            return
        spec = importlib.util.spec_from_file_location("extract_mocha", script)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader
        spec.loader.exec_module(mod)
        pdf = Path(opts["pdf"] or mod.PDF_DEFAULT)
        if not pdf.is_file():
            self.stderr.write(self.style.ERROR(f"PDF 不存在: {pdf}"))
            return
        r = subprocess.run([sys.executable, str(script), str(pdf)], cwd=str(root))
        if r.returncode != 0:
            self.stderr.write(self.style.ERROR("extract_mocha_pdf_cards 失败"))
            return
        self.stdout.write(self.style.SUCCESS("已完成 PDF 提取与导入"))
