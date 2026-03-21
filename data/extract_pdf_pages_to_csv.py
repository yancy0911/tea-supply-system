#!/usr/bin/env python3
"""
从 MOCHA PDF 逐页提取有明确价格的商品，写入 data/products_import_ready.csv，
并支持行号→页码映射以按页统计与按页顺序写入。
"""
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:
    print("需要: pip install pypdf", file=sys.stderr)
    raise

BASE = Path(__file__).resolve().parent
PDF_DEFAULT = Path("/Users/tingtingfu/Desktop/2025 MOCHA目录-5月更新.pdf")
OUT = BASE / "products_import_ready.csv"

HEADER = [
    "category",
    "name",
    "sku",
    "unit_label",
    "case_label",
    "price_single",
    "price_case",
    "shelf_life_months",
    "can_split_sale",
    "minimum_order_qty",
    "is_active",
]

SKU_ANY = re.compile(r"SKU\s*#\s*[:：]\s*([A-Z0-9]+)", re.I)

SECTION_HEADERS = (
    "Tea Leaves ",
    "Filtered Tea Bag",
    "Espresso Tea Bag",
    "Sugar Syrup",
    "Creamer ",
    "Tapioca Boba",
    "Tropical Fruit Syrup",
    "Pulp Topping",
    "Tropical Fruit Jam",
    "Pure Powder",
    "Special  Powder",
    "Special Powder",
    "Jelly 椰果",
    "Popping Boba",
    "Canned Topping",
    "Agar Boba",
    "Machinery ",
    "Sealing Film",
    "Individually Wrap Straw",
    "Tools ",
    "Bag & Hand Carrier",
    "Lid 注塑连体杯盖",
    "Red Heart",
    "PP 1 Oz",
    "PP 530ml",
    "PP700ml",
    "PP 700ml",
    "Strainer ",
    "PC2oz",
    "PC Double",
)

SKIP_NAME_PREFIXES = (
    "www.",
    "tel:",
    "wechat",
    "56-11",
    "墨茶",
    "mochaboba",
)


def parse_money_pair(s):
    s = s.strip()
    s = re.sub(r"\$", " ", s)
    nums = re.findall(r"[\d,]+\.?\d*", s)
    out = []
    for x in nums:
        try:
            out.append(float(x.replace(",", "")))
        except ValueError:
            pass
    if not out:
        return None
    if len(out) >= 2:
        return out[0], out[1]
    return out[0], out[0]


def is_meta_line(s):
    u = s.upper().strip()
    if u.startswith("PRICE"):
        return True
    if u.startswith("CASE"):
        return True
    if u.startswith("SINGLE"):
        return True
    if u.startswith("SHELF"):
        return True
    if u.startswith("INSULATED") or u.startswith("BLACK DOME"):
        return True
    return False


def is_section_header(s):
    st = s.strip()
    return any(st.startswith(p) for p in SECTION_HEADERS)


def prev_line_with_sku(lines, sku_line_idx):
    for j in range(sku_line_idx - 1, -1, -1):
        if SKU_ANY.search(lines[j]):
            return j
    return -1


def parse_meta_from_lines(segment):
    case_label = ""
    unit_label = ""
    shelf_m = 12
    ps, pc = None, None
    for s in segment:
        m = re.search(r"CASE\s*:\s*(.+)", s, re.I)
        if m:
            case_label = m.group(1).strip()
        m = re.search(r"SINGLE\s*:\s*(.+)", s, re.I)
        if m:
            unit_label = m.group(1).strip()
        u = s.upper()
        m = re.search(r"SHELF\s*(?:LIFE)?\s*[:：]\s*(\d+)\s*MONTHS?", u.replace("：", ":"))
        if m:
            shelf_m = int(m.group(1))
        m = re.search(r"PRICE\s*[:：]\s*(.+)", s, re.I)
        if m:
            pr = parse_money_pair(m.group(1))
            if pr:
                ps, pc = pr
    return case_label, unit_label, shelf_m, ps, pc


def last_complete_pack_block(segment):
    last_pi = None
    for i in range(len(segment) - 1, -1, -1):
        if re.search(r"PRICE\s*[:：]", segment[i], re.I):
            last_pi = i
            break
    if last_pi is None:
        return []
    start = None
    for i in range(last_pi, -1, -1):
        if re.search(r"CASE\s*:", segment[i], re.I):
            start = i
            break
    if start is None:
        return []
    return segment[start : last_pi + 1]


def extract_fields_after_sku(lines, start):
    case_label = ""
    unit_label = ""
    shelf_m = 12
    ps, pc = None, None
    k = start + 1
    while k < len(lines) and k < start + 35:
        s = lines[k]
        if SKU_ANY.search(s):
            break
        m = re.search(r"CASE\s*:\s*(.+)", s, re.I)
        if m:
            case_label = m.group(1).strip()
        m = re.search(r"SINGLE\s*:\s*(.+)", s, re.I)
        if m:
            unit_label = m.group(1).strip()
        u = s.upper()
        m = re.search(r"SHELF\s*(?:LIFE)?\s*[:：]\s*(\d+)\s*MONTHS?", u.replace("：", ":"))
        if m:
            shelf_m = int(m.group(1))
        m = re.search(r"PRICE\s*[:：]\s*(.+)", s, re.I)
        if m:
            pr = parse_money_pair(m.group(1))
            if pr:
                ps, pc = pr
        k += 1
    return case_label, unit_label, shelf_m, ps, pc


def fill_from_prev_sku_block(lines, sku_line_idx, case_label, unit_label, shelf_m, ps, pc):
    prev = prev_line_with_sku(lines, sku_line_idx)
    if prev < 0:
        lo = max(0, sku_line_idx - 28)
    else:
        lo = prev + 1
    hi = sku_line_idx
    segment = lines[lo:hi]
    pack = last_complete_pack_block(segment)
    if not pack:
        return case_label, unit_label, shelf_m, ps, pc
    c2, u2, sh2, p2, q2 = parse_meta_from_lines(pack)
    if ps is not None:
        return case_label, unit_label, shelf_m, ps, pc
    if p2 is None:
        return case_label, unit_label, shelf_m, ps, pc
    ps, pc = p2, q2
    if not case_label and c2:
        case_label = c2
    if not unit_label and u2:
        unit_label = u2
    if shelf_m == 12 and sh2 != 12:
        shelf_m = sh2
    return case_label, unit_label, shelf_m, ps, pc


def should_skip_line(s):
    sl = s.strip().lower()
    return any(sl.startswith(p) for p in SKIP_NAME_PREFIXES)


def is_junk_sku_before_section(lines, i):
    if i + 1 >= len(lines):
        return False
    return is_section_header(lines[i + 1].strip())


def emit_product(rows, skipped, sku, name_parts, current_cat, lines, line_idx, sku_line_idx_for_page):
    if is_junk_sku_before_section(lines, line_idx):
        return None

    parts = [re.sub(r"\s+", " ", p) for p in name_parts if p.strip()]
    name_str = " ".join(parts).strip()
    if not name_str:
        name_str = sku

    case_label, unit_label, shelf_m, ps, pc = extract_fields_after_sku(lines, line_idx)
    case_label, unit_label, shelf_m, ps, pc = fill_from_prev_sku_block(
        lines, line_idx, case_label, unit_label, shelf_m, ps, pc
    )

    if ps is None:
        skipped.append((sku, "no price", line_idx + 1))
        return None

    # 整箱价没有则等于单品价
    if pc is None:
        pc = ps

    rows.append(
        {
            "category": current_cat or "默认分类",
            "name": name_str[:200],
            "sku": sku,
            "unit_label": (unit_label or "")[:120],
            "case_label": (case_label or "")[:120],
            "price_single": ps,
            "price_case": pc,
            "shelf_life_months": shelf_m if shelf_m else 12,
            "_sku_line_idx": sku_line_idx_for_page,
        }
    )
    return name_str


def parse_full_catalog(lines):
    """返回 rows（含 _sku_line_idx 用于页码），skipped"""
    current_cat = "默认分类"
    section_title = ""
    name_accum = []
    last_name = ""

    rows = []
    skipped = []

    for i, raw_line in enumerate(lines):
        s = raw_line.strip()

        if should_skip_line(s):
            continue

        if is_section_header(raw_line):
            current_cat = re.sub(r"\s+", " ", s)[:100]
            name_accum = []
            last_name = ""
            section_title = s[:80] if len(s) < 90 else ""
            continue

        if not SKU_ANY.search(raw_line):
            if s and not is_meta_line(s):
                name_accum.append(s)
            continue

        matches = list(SKU_ANY.finditer(raw_line))
        name_pool = list(name_accum)
        base = " ".join(re.sub(r"\s+", " ", p) for p in name_pool).strip()
        if not base:
            base = last_name
        if not base:
            base = section_title.strip()
        if not base:
            base = ""

        for m in matches:
            sku = m.group(1)
            nm = base if base else sku
            done = emit_product(rows, skipped, sku, [nm], current_cat, lines, i, i)
            if done:
                last_name = done

        name_accum = []

    return rows, skipped


def build_lines_and_page_map(pdf_path: Path):
    reader = PdfReader(str(pdf_path))
    all_lines = []
    line_to_page = []
    for pnum, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        for line in text.splitlines():
            all_lines.append(line)
            line_to_page.append(pnum)
    return all_lines, line_to_page


def main():
    pdf_path = PDF_DEFAULT
    if len(sys.argv) > 1:
        pdf_path = Path(sys.argv[1])
    if not pdf_path.is_file():
        print(f"找不到 PDF: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"读取 PDF: {pdf_path}")
    lines, line_to_page = build_lines_and_page_map(pdf_path)
    total_pages = max(line_to_page) if line_to_page else 0
    print(f"共 {total_pages} 页，{len(lines)} 行文本")

    rows, skipped = parse_full_catalog(lines)

    # 每行对应页码
    def page_for_row(r):
        idx = r.get("_sku_line_idx", 0)
        if 0 <= idx < len(line_to_page):
            return line_to_page[idx]
        return 0

    for r in rows:
        r["_page"] = page_for_row(r)

    # 同 SKU 去重：保留最后一次出现
    by_sku = {}
    for r in rows:
        by_sku[r["sku"]] = r

    final_rows = list(by_sku.values())

    # 按页分组（用于统计与按页顺序写出）
    by_page = defaultdict(list)
    for r in final_rows:
        by_page[r["_page"]].append(r)

    # 写出 CSV：先表头，再按页码顺序、页内按 sku 排序（同 SKU 多页出现时保留最后一次，归入该次所在页）
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for p in range(1, total_pages + 1):
            page_rows = sorted(by_page.get(p, []), key=lambda x: x["sku"])
            n = len(page_rows)
            for r in page_rows:
                w.writerow(
                    {
                        "category": r["category"],
                        "name": r["name"],
                        "sku": r["sku"],
                        "unit_label": r["unit_label"],
                        "case_label": r["case_label"],
                        "price_single": r["price_single"],
                        "price_case": r["price_case"],
                        "shelf_life_months": r.get("shelf_life_months") or 12,
                        "can_split_sale": 1,
                        "minimum_order_qty": 1,
                        "is_active": 1,
                    }
                )
            hint = "（封面/信息页，无带价 SKU 块）" if p == 1 and n == 0 else "（本页 SKU 行对应商品，去重后计入）"
            print(f"第 {p} 页：新增 {n} 个商品{hint}")

    print(f"\n合计写入 {len(final_rows)} 条到 {OUT}")
    print(f"解析跳过（无明确价格等）: {len(skipped)} 条 SKU 记录")
    if skipped:
        for x in skipped[:15]:
            print("  ", x)

    # 自动导入数据库
    import subprocess

    root = BASE.parent
    print("\n执行: python manage.py import_products_ready")
    r = subprocess.run(
        [sys.executable, str(root / "manage.py"), "import_products_ready"],
        cwd=str(root),
    )
    if r.returncode != 0:
        sys.exit(r.returncode)


if __name__ == "__main__":
    main()
