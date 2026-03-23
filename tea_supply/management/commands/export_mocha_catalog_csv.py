"""
从 MOCHA 目录 PDF 导出完整商品 CSV（与后台 Import 表头一致）：data/products_import_ready.csv
可选导出配图到 media/products/（默认开启）。
"""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "从 MOCHA PDF 导出 data/products_import_ready.csv（整本目录，200+ 条）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--pdf",
            type=str,
            default="",
            help="PDF 路径；留空则依次尝试 Desktop 默认名与 tea_supply/data/mocha.pdf",
        )
        parser.add_argument(
            "--no-images",
            action="store_true",
            help="不导出配图，仅写 CSV（image 列为空或占位路径）",
        )

    def handle(self, *args, **opts):
        base = Path(settings.BASE_DIR)
        data_dir = base / "data"
        mod_path = data_dir / "extract_mocha_pdf_cards.py"
        spec = importlib.util.spec_from_file_location("extract_mocha_pdf_cards", mod_path)
        if spec is None or spec.loader is None:
            raise CommandError(f"找不到 {mod_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        pdf = (opts.get("pdf") or "").strip()
        candidates = []
        if pdf:
            candidates.append(Path(pdf).expanduser().resolve())
        candidates.append(Path.home() / "Desktop" / "2025 MOCHA目录-5月更新.pdf")
        candidates.append(base / "tea_supply" / "data" / "mocha.pdf")

        pdf_path = None
        for p in candidates:
            if p.is_file():
                pdf_path = p
                break
        if not pdf_path:
            raise CommandError(
                "未找到 PDF。请用 --pdf 指定，或将文件放到 Desktop/2025 MOCHA目录-5月更新.pdf "
                "或 tea_supply/data/mocha.pdf"
            )

        skip_images = bool(opts.get("no_images"))
        self.stdout.write(f"解析: {pdf_path}（导出图片: {not skip_images}）")
        cards, failures = mod.extract_catalog(pdf_path, skip_images=skip_images)
        final = mod.dedupe_cards(cards)
        out_csv = data_dir / "products_import_ready.csv"
        fail_path = data_dir / "pdf_import_failures.txt"
        header = mod.HEADER

        with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=header)
            w.writeheader()
            for c in sorted(final, key=lambda x: x["sku"]):
                w.writerow(
                    {
                        "category": c["category"],
                        "name": c["name"],
                        "sku": c["sku"],
                        "unit_label": c["unit_label"],
                        "case_label": c["case_label"],
                        "price_single": c["price_single"],
                        "price_case": c["price_case"],
                        "shelf_life_months": c.get("shelf_life_months") or 12,
                        "can_split_sale": 1,
                        "minimum_order_qty": 1,
                        "is_active": 1,
                        "image": c.get("image") or "",
                    }
                )

        with fail_path.open("w", encoding="utf-8") as ff:
            ff.write("sku\tpage\tline\terror\n")
            for sku, pg, ln, err in failures:
                ff.write(f"{sku}\t{pg}\t{ln}\t{err}\n")

        self.stdout.write(
            self.style.SUCCESS(
                f"已写入 {out_csv}（{len(final)} 条商品）；失败明细 {fail_path}（{len(failures)} 行）"
            )
        )
        self.stdout.write(
            "下一步：后台 商品 → Import，上传该 CSV；配图在 media/products/（部署时需持久化 MEDIA）。"
        )
