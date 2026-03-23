"""
从 MOCHA 目录 PDF 按 SKU 邻域切图 → media/products/{sku}.jpg，并写入 Product.image。

用法:
  python manage.py bind_images_from_pdf
  python manage.py bind_images_from_pdf --pdf /path/to/catalog.pdf

优先级: --pdf > 环境变量 MOCHA_PDF > <BASE>/data/mocha_catalog.pdf > data/extract_mocha_pdf_cards.PDF_DEFAULT
"""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from tea_supply.models import Product


def _load_extractor():
    path = Path(settings.BASE_DIR) / "data" / "extract_mocha_pdf_cards.py"
    spec = importlib.util.spec_from_file_location("extract_mocha_pdf_cards", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


class Command(BaseCommand):
    help = "从 PDF 切商品图为 JPG 并绑定到 Product.image"

    def add_arguments(self, parser):
        parser.add_argument(
            "--pdf",
            default=None,
            help="商品目录 PDF 路径",
        )

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        mod = _load_extractor()
        pdf_arg = opts.get("pdf")
        pdf_path = None
        if pdf_arg:
            pdf_path = Path(pdf_arg).expanduser()
        if not pdf_path or not pdf_path.is_file():
            envp = (os.environ.get("MOCHA_PDF") or "").strip()
            if envp:
                pdf_path = Path(envp).expanduser()
        if not pdf_path or not pdf_path.is_file():
            cand = base / "data" / "mocha_catalog.pdf"
            if cand.is_file():
                pdf_path = cand
        if not pdf_path or not pdf_path.is_file():
            default_pdf = getattr(mod, "PDF_DEFAULT", None)
            if default_pdf and Path(default_pdf).is_file():
                pdf_path = Path(default_pdf)

        if not pdf_path or not pdf_path.is_file():
            self.stderr.write(
                self.style.ERROR(
                    "未找到 PDF。请使用 --pdf 指定，或设置 MOCHA_PDF，"
                    "或将文件放在 data/mocha_catalog.pdf"
                )
            )
            return

        media_products = Path(settings.MEDIA_ROOT) / "products"
        self.stdout.write(f"PDF: {pdf_path}")
        self.stdout.write(f"输出目录: {media_products} （*.jpg）")

        try:
            success_skus, pdf_skus_ordered = mod.export_pdf_product_images_jpg(
                pdf_path, media_products=media_products
            )
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"解析 PDF 失败: {exc}"))
            return

        bound = 0
        for sku in success_skus:
            try:
                n = Product.objects.filter(sku=sku).update(image=f"products/{sku}.jpg")
                if n:
                    bound += int(n)
            except Exception:
                continue

        db_skus = set(Product.objects.values_list("sku", flat=True))
        unmatched = sorted(db_skus - set(success_skus))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS(f"成功写出 JPG 的 SKU 数（PDF）: {len(success_skus)}"))
        self.stdout.write(self.style.SUCCESS(f"成功绑定到数据库 Product.image 的条数: {bound}"))
        self.stdout.write("")
        self.stdout.write(
            "PDF 中出现过的 SKU（去重顺序，前 20 个）: "
            + ", ".join(pdf_skus_ordered[:20])
            + (" …" if len(pdf_skus_ordered) > 20 else "")
        )
        self.stdout.write("")
        if unmatched:
            self.stdout.write(
                self.style.WARNING(
                    "数据库中本次未匹配到切图的 SKU（共 %d 个，保留原图/默认图）:\n%s"
                    % (len(unmatched), ", ".join(unmatched))
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS("数据库中所有商品 SKU 均在本次 PDF 切图中得到文件。"))
