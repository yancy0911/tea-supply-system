"""
从 MOCHA PDF 解析价格，仅更新数据库中与 PDF 不一致的商品（不跑全量 CSV 导入）。
"""
import importlib.util
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from tea_supply.models import Product


def _load_extract_module():
    root = Path(settings.BASE_DIR)
    script = root / "data" / "extract_mocha_pdf_cards.py"
    spec = importlib.util.spec_from_file_location("extract_mocha_pdf", script)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _dedupe_cards(cards: list[dict]) -> dict[str, dict]:
    by_sku: dict[str, dict] = {}
    for c in cards:
        by_sku[c["sku"]] = c
    return by_sku


def _needs_update(db_single: float, db_case: float, ps: float, pc: float) -> bool:
    eps = 0.02
    return abs(float(db_single) - float(ps)) > eps or abs(float(db_case) - float(pc)) > eps


class Command(BaseCommand):
    help = "按 SKU 从 PDF 提取价格，仅更新与 PDF 不一致的 Product.price_single / price_case"

    def add_arguments(self, parser):
        parser.add_argument(
            "--pdf",
            default=None,
            help="PDF 路径（默认与 data/extract_mocha_pdf_cards.py 中 PDF_DEFAULT 一致）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只打印将修改的 SKU，不写数据库",
        )

    def handle(self, *args, **opts):
        mod = _load_extract_module()
        pdf_path = Path(opts["pdf"] or mod.PDF_DEFAULT)
        if not pdf_path.is_file():
            self.stderr.write(self.style.ERROR(f"找不到 PDF: {pdf_path}"))
            return

        self.stdout.write(f"解析 PDF（仅价格，不导出图片）: {pdf_path}")
        cards, failures = mod.extract_catalog(pdf_path, skip_images=True)
        pdf_by_sku = _dedupe_cards(cards)

        to_update: list[Product] = []
        report: list[tuple[str, float, float, float, float]] = []

        for p in Product.objects.all().only("id", "sku", "price_single", "price_case"):
            c = pdf_by_sku.get(p.sku.strip())
            if not c:
                continue
            ps = float(c["price_single"])
            pc = float(c["price_case"])
            if not _needs_update(p.price_single, p.price_case, ps, pc):
                continue
            report.append((p.sku, float(p.price_single), float(p.price_case), ps, pc))
            p.price_single = ps
            p.price_case = pc
            to_update.append(p)

        if not report:
            self.stdout.write(self.style.SUCCESS("无价格差异，数据库已与 PDF 一致（或可匹配 SKU 无变化）。"))
            return

        self.stdout.write(self.style.WARNING(f"将修正 {len(report)} 条价格："))
        for sku, o_s, o_c, n_s, n_c in report:
            self.stdout.write(f"  {sku}: 单品 {o_s}→{n_s}  整箱 {o_c}→{n_c}")

        if opts["dry_run"]:
            self.stdout.write(self.style.WARNING("dry-run：未写入数据库"))
            return

        with transaction.atomic():
            Product.objects.bulk_update(to_update, ["price_single", "price_case"])

        self.stdout.write(self.style.SUCCESS(f"已更新 {len(to_update)} 条商品的价格字段。"))
        if failures:
            self.stdout.write(
                self.style.WARNING(
                    f"注意：PDF 中有 {len(failures)} 条解析失败条目未进入价格表，不影响本次 SKU 匹配更新。"
                )
            )
