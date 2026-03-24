"""
从 MOCHA 目录 PDF 一键导入商品：解析单价、自动整箱价、中文分类、SKU=T001…
依赖：PyMuPDF（requirements 已含）、data/extract_mocha_pdf_cards.py
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from tea_supply.models import Product, ProductCategory

# 按优先级：更具体的规则在前（名称与 tea_supply.category_names 标准英文一致）
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("Tea Leaves", "Filtered Tea", "Espresso Tea", "Tea Bag"), "Tea"),
    (("Creamer",), "Dairy & creamer"),
    (("Tropical Fruit Jam", "Fruit Jam", "Jam"), "Fruit Jam"),
    (("Pulp Topping", "Pulp"), "Pulp & toppings"),
    (("Sugar Syrup", "Tropical Fruit Syrup"), "Syrup"),
    (("Pure Powder", "Special Powder", "Powder"), "Powders"),
    (("Tapioca", "Popping Boba", "Agar Boba", "Jelly 椰果", "Jelly"), "Boba & toppings"),
    (("Canned Topping", "Canned"), "Canned toppings"),
    (
        (
            "Machinery",
            "Sealing Film",
            "Individually Wrap Straw",
            "Tools",
            "Bag & Hand Carrier",
            "Lid",
            "Strainer",
            "Straw",
            "Sealing",
            "Wrap",
            "Red Heart",
        ),
        "Packaging & tools",
    ),
    (("PP 1 Oz", "PP 530ml", "PP700ml", "PP 700ml", "PC2oz", "PC Double"), "Packaging & tools"),
)


def _normalize_category_name(raw: str) -> str:
    s = re.sub(r"\s+", " ", (raw or "").strip())
    if not s:
        return "Default"
    up = s.upper()
    for keys, label in _CATEGORY_RULES:
        for k in keys:
            if k.upper() in up or k.lower() in s.lower():
                return label
    low = s.lower()
    if any(w in low for w in ("tea", "乌龙", "红茶", "绿茶")):
        return "Tea"
    if "creamer" in low or "奶精" in s:
        return "Dairy & creamer"
    if "syrup" in low:
        return "Syrup"
    if "jam" in low:
        return "Fruit Jam"
    if "powder" in low:
        return "Powders"
    if "boba" in low or "tapioca" in low or "椰果" in s:
        return "Boba & toppings"
    return s[:100] if len(s) <= 100 else s[:97] + "…"


def _load_extract_catalog(data_dir: Path):
    path = data_dir / "extract_mocha_pdf_cards.py"
    spec = importlib.util.spec_from_file_location("extract_mocha_pdf_cards", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "extract_catalog") or not hasattr(mod, "dedupe_cards"):
        raise ImportError("extract_mocha_pdf_cards 缺少 extract_catalog / dedupe_cards")
    return mod.extract_catalog, mod.dedupe_cards


def _format_t_sku(n: int) -> str:
    return f"T{n:03d}"


def _parse_max_t_sku_number() -> int:
    best = 0
    for sku in Product.objects.filter(sku__startswith="T").values_list("sku", flat=True):
        m = re.match(r"^T(\d+)$", sku, re.I)
        if m:
            best = max(best, int(m.group(1)))
    return best


# 新建分类时的排序（越小越靠前）
_CATEGORY_SORT = {
    "Tea": 10,
    "Dairy & creamer": 20,
    "Fruit Jam": 30,
    "Pulp & toppings": 35,
    "Syrup": 40,
    "Powders": 50,
    "Boba & toppings": 60,
    "Canned toppings": 70,
    "Packaging & tools": 80,
    "Default": 900,
}


class Command(BaseCommand):
    help = "从 MOCHA PDF 导入商品（自动分类、T 系列 SKU、整箱价=单价×箱规）"

    def add_arguments(self, parser):
        default_pdf = Path(settings.BASE_DIR) / "tea_supply" / "data" / "mocha.pdf"
        parser.add_argument(
            "--pdf",
            type=str,
            default=str(default_pdf),
            help="PDF 路径（默认: 项目内 tea_supply/data/mocha.pdf）",
        )
        parser.add_argument(
            "--units-per-case",
            type=float,
            default=8.0,
            help="整箱价 = 单价 × 该系数；同时写入 Product.units_per_case（默认 8）",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="只解析并打印统计，不写数据库",
        )
        parser.add_argument(
            "--update-existing",
            action="store_true",
            help="若存在同名同分类商品则更新价格（不分配新 SKU）",
        )

    def handle(self, *args, **opts):
        pdf_path = Path(opts["pdf"]).expanduser().resolve()
        expected = Path(settings.BASE_DIR) / "tea_supply" / "data" / "mocha.pdf"
        if not pdf_path.is_file():
            raise CommandError(
                f"找不到 PDF 文件: {pdf_path}\n"
                f"请将 MOCHA 目录 PDF 放入项目固定路径后重试: {expected}"
            )

        units = float(opts["units_per_case"])
        if units <= 0:
            raise CommandError("--units-per-case 必须大于 0")

        data_dir = Path(settings.BASE_DIR) / "data"
        extract_catalog, dedupe_cards = _load_extract_catalog(data_dir)

        self.stdout.write(f"解析 PDF（跳过导出图片，主图留空）: {pdf_path}")
        cards, failures = extract_catalog(pdf_path, skip_images=True)
        final = dedupe_cards(cards)
        self.stdout.write(f"解析到商品卡片: {len(final)}，解析失败行: {len(failures)}")

        if opts["dry_run"]:
            for c in final[:5]:
                zh = _normalize_category_name(c["category"])
                ps = c.get("price_single")
                self.stdout.write(f"  样例: [{zh}] {c['name'][:40]}… 单价={ps}")
            self.stdout.write(self.style.WARNING("dry-run 结束，未写入数据库"))
            return

        seq = _parse_max_t_sku_number()
        created = 0
        updated = 0
        cats_created = set()

        with transaction.atomic():
            for card in sorted(final, key=lambda x: (x.get("page", 0), str(x.get("sku", "")))):
                raw_cat = card.get("category") or ""
                cat_name = _normalize_category_name(raw_cat)
                sort_order = _CATEGORY_SORT.get(cat_name, 500)

                category, cat_created = ProductCategory.objects.get_or_create(
                    name=cat_name,
                    defaults={"sort_order": sort_order, "is_active": True},
                )
                if cat_created:
                    cats_created.add(cat_name)
                elif category.sort_order != sort_order and cat_name in _CATEGORY_SORT:
                    category.sort_order = sort_order
                    category.save(update_fields=["sort_order"])

                name = (card.get("name") or "").strip()[:200]
                if not name:
                    continue

                ps = float(card["price_single"])
                pc = round(ps * units, 2)

                shelf = int(card.get("shelf_life_months") or 12)
                unit_label = (card.get("unit_label") or "")[:120]
                case_label = (card.get("case_label") or "")[:120]

                existing = None
                if opts["update_existing"]:
                    existing = Product.objects.filter(category=category, name=name).first()

                fields = {
                    "category": category,
                    "name": name,
                    "unit_label": unit_label,
                    "case_label": case_label,
                    "price_single": ps,
                    "price_case": pc,
                    "cost_price_single": 0.0,
                    "cost_price_case": 0.0,
                    "shelf_life_months": max(1, min(shelf, 120)),
                    "can_split_sale": True,
                    "minimum_order_qty": 1.0,
                    "is_active": True,
                    "image": "",
                    "units_per_case": units,
                    "stock_quantity": 0.0,
                }

                if existing:
                    for k, v in fields.items():
                        if k == "category":
                            continue
                        setattr(existing, k, v)
                    existing.save()
                    updated += 1
                else:
                    seq += 1
                    sku = _format_t_sku(seq)
                    Product.objects.create(sku=sku, **fields)
                    created += 1

        self.stdout.write(self.style.SUCCESS("—— 导入完成 ——"))
        self.stdout.write(f"新建商品: {created}，更新商品: {updated}")
        self.stdout.write(f"新建分类: {len(cats_created)} {sorted(cats_created)}")
        self.stdout.write(f"整箱价公式: 单价 × {units}")
        if failures:
            self.stdout.write(
                self.style.WARNING(
                    f"PDF 内未解析行数（无 PRICE 等）: {len(failures)}，可查看 data/pdf_import_failures.txt（若用脚本独立跑会生成）"
                )
            )
