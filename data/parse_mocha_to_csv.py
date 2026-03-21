#!/usr/bin/env python3
"""Parse MOCHA catalog extracted text -> products_import_ready.csv"""
import csv
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent
TEXT = BASE / "mocha_catalog_extracted.txt"
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

# Matches all SKU tokens on a line: "SKU # :X", "SKU#:X", fullwidth colon
SKU_ANY = re.compile(r"SKU\s*#\s*[:：]\s*([A-Z0-9]+)", re.I)
SKU_LINE_ONLY = re.compile(r"^\s*SKU\s*#\s*[:：]\s*([A-Z0-9]+)\s*$", re.I)

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
    """segment: list of line strings"""
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
    """
    If the segment ends with a CASE..SINGLE..SHELF..PRICE block, return that sub-segment only.
    Avoids attaching the previous SKU's trailing PRICE when only one price appears between SKUs.
    """
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
    """When forward block has no price, use orphan CASE..PRICE block between previous SKU and this SKU."""
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
    """PDF artifact: stray SKU line immediately before a new section (e.g. duplicate B030702)."""
    if i + 1 >= len(lines) and is_section_header(lines[i + 1].strip()):
        return True
    return False


def emit_product(rows, skipped, sku, name, current_cat, lines, line_idx):
    if is_junk_sku_before_section(lines, line_idx):
        return None

    parts = [re.sub(r"\s+", " ", p) for p in name if p.strip()]
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

    rows.append(
        {
            "category": current_cat,
            "name": name_str[:200],
            "sku": sku,
            "unit_label": unit_label[:120],
            "case_label": case_label[:120],
            "price_single": ps,
            "price_case": pc,
            "shelf_life_months": shelf_m,
        }
    )
    return name_str


def main():
    raw = TEXT.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()

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

        # Line contains at least one SKU
        matches = list(SKU_ANY.finditer(raw_line))
        name_pool = list(name_accum)
        base = " ".join(re.sub(r"\s+", " ", p) for p in name_pool).strip()
        if not base:
            base = last_name
        if not base:
            base = section_title.strip()
        if not base:
            base = ""

        names_for = [base] if base else [""]

        for mi, m in enumerate(matches):
            sku = m.group(1)
            nm = names_for[0] if names_for[0] else sku
            done = emit_product(rows, skipped, sku, [nm], current_cat, lines, i)
            if done:
                last_name = done

        name_accum = []

    by_sku = {}
    for r in rows:
        by_sku[r["sku"]] = r
    final = list(by_sku.values())

    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for r in sorted(final, key=lambda x: x["sku"]):
            w.writerow(
                {
                    "category": r["category"],
                    "name": r["name"],
                    "sku": r["sku"],
                    "unit_label": r["unit_label"],
                    "case_label": r["case_label"],
                    "price_single": r["price_single"],
                    "price_case": r["price_case"],
                    "shelf_life_months": r["shelf_life_months"],
                    "can_split_sale": 1,
                    "minimum_order_qty": 1,
                    "is_active": 1,
                }
            )

    print(f"Wrote {len(final)} products to {OUT}")
    print(f"Skipped (no price): {len(skipped)}")
    for x in skipped[:35]:
        print("  ", x)


if __name__ == "__main__":
    main()
