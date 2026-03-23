#!/usr/bin/env python3
"""将项目内 products_import_ready.csv 规范为 UTF-8 逗号分隔列，写入 /tmp/products.csv。"""
from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "data" / "products_import_ready.csv"
PDF_DEFAULT = Path("/Users/tingtingfu/Desktop/2025 MOCHA目录-5月更新.pdf")
OUT_PRODUCTS = Path("/tmp/products.csv")
OUT_CATEGORIES = Path("/tmp/product_categories.csv")

HEADER = [
    "category",
    "name",
    "sku",
    "unit_label",
    "case_label",
    "price_single",
    "price_case",
    "cost_price_single",
    "cost_price_case",
    "shelf_life_months",
    "can_split_sale",
    "minimum_order_qty",
    "is_active",
    "stock_quantity",
    "units_per_case",
    "image",
]

CAT_HEADER = ["name", "sort_order", "is_active"]


def _cell(s: str) -> str:
    t = str(s).strip()
    t = t.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
    while "  " in t:
        t = t.replace("  ", " ")
    return t


def main() -> int:
    if not SRC.is_file():
        pdf = PDF_DEFAULT if PDF_DEFAULT.is_file() else None
        if not pdf:
            print(f"缺少 {SRC} 且未找到 PDF: {PDF_DEFAULT}", file=sys.stderr)
            return 1
        r = subprocess.run(
            [sys.executable, str(BASE / "data" / "extract_mocha_pdf_cards.py"), str(pdf), "--csv-only"],
            cwd=str(BASE),
        )
        if r.returncode != 0 or not SRC.is_file():
            return 1

    with SRC.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            raw_h = next(reader)
        except StopIteration:
            print("CSV 为空", file=sys.stderr)
            return 1
        file_header = [h.strip() for h in raw_h]
        if file_header != HEADER:
            print(f"表头不符，期望 {HEADER}\n实际 {file_header}", file=sys.stderr)
            return 1
        body: list[list[str]] = []
        for row in reader:
            cells = [_cell(c) for c in row]
            while len(cells) < len(HEADER):
                cells.append("")
            cells = cells[: len(HEADER)]
            if not any(cells):
                continue
            body.append(cells)

    # 从商品 CSV 的 category 列去重，生成后台 ProductCategory 导入文件
    seen: set[str] = set()
    cat_order: list[str] = []
    cat_col = HEADER.index("category")
    for row in body:
        if len(row) <= cat_col:
            continue
        c = _cell(row[cat_col])
        if not c:
            c = "默认分类"
        if c not in seen:
            seen.add(c)
            cat_order.append(c)
    cat_order.sort(key=lambda s: s.lower())

    OUT_CATEGORIES.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CATEGORIES.open("w", encoding="utf-8", newline="") as f:
        cw = csv.writer(
            f,
            delimiter=",",
            quoting=csv.QUOTE_MINIMAL,
            doublequote=True,
            lineterminator="\n",
        )
        cw.writerow(CAT_HEADER)
        for i, name in enumerate(cat_order):
            cw.writerow([name, str(i), "1"])

    OUT_PRODUCTS.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PRODUCTS.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(
            f,
            delimiter=",",
            quoting=csv.QUOTE_MINIMAL,
            doublequote=True,
            lineterminator="\n",
        )
        w.writerow(HEADER)
        for row in body:
            w.writerow(row)

    print(f"Wrote {OUT_CATEGORIES} ({len(cat_order)} categories)")
    print(f"Wrote {OUT_PRODUCTS} ({len(body)} data rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
