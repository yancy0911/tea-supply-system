#!/usr/bin/env python3
"""
按「页 + 商品卡片块」从 MOCHA PDF 提取：SKU 至 PRICE 为同一卡片元数据，
名称块为上一卡片 PRICE 之后到本 SKU 之前；价格不跨卡片串用。
导出图片到 media/products/{sku}.png，并生成 products_import_ready.csv。
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import fitz  # PyMuPDF

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
MEDIA_PRODUCTS = ROOT / "media" / "products"
OUT_CSV = BASE / "products_import_ready.csv"
FAILURES = BASE / "pdf_import_failures.txt"
PDF_DEFAULT = Path("/Users/tingtingfu/Desktop/2025 MOCHA目录-5月更新.pdf")

# 与 tea_supply.models.Product / import_products_ready / 后台 CSV 一致：标准逗号分隔 CSV
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

SKU_ANY = re.compile(r"SKU\s*#\s*[:：]\s*([A-Z0-9]+)", re.I)
PRICE_RE = re.compile(r"PRICE\s*[:：]\s*(.+)", re.I)
CASE_RE = re.compile(r"CASE\s*:\s*(.+)", re.I)
SINGLE_RE = re.compile(r"SINGLE\s*:\s*(.+)", re.I)
SHELF_RE = re.compile(r"SHELF\s*(?:LIFE)?\s*[:：]\s*(\d+)\s*MONTHS?", re.I)

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


def parse_units_per_case(case_label: str) -> float:
    """从 CASE 行文案中解析「每箱件数」，供 units_per_case；缺省为 1。"""
    if not case_label or not str(case_label).strip():
        return 1.0
    s = str(case_label).strip()
    m = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:BAGS?|BOTTLES?|CANS?|BOXES?|PCS|PACKS?|UNITS?)/\s*CASE",
        s,
        re.I,
    )
    if m:
        return float(m.group(1))
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if nums:
        return float(nums[0])
    return 1.0


def parse_money_pair(s: str):
    s = re.sub(r"\$", " ", s.strip())
    nums = re.findall(r"[\d,]+\.?\d*", s)
    out = []
    for x in nums:
        try:
            out.append(float(x.replace(",", "")))
        except ValueError:
            pass
    if not out:
        return None, None
    if len(out) >= 2:
        return out[0], out[1]
    return out[0], out[0]


def is_meta_line(s: str) -> bool:
    u = s.upper().strip()
    return (
        u.startswith("PRICE")
        or u.startswith("CASE")
        or u.startswith("SINGLE")
        or u.startswith("SHELF")
        or u.startswith("INSULATED")
        or u.startswith("BLACK DOME")
    )


def is_section_header_line(s: str) -> bool:
    st = s.strip()
    return any(st.startswith(p) for p in SECTION_HEADERS)


def extract_ordered_lines(page: fitz.Page) -> list[dict]:
    """页面内按阅读顺序（先上后下、先左后右）的文本行 + bbox。"""
    out = []
    d = page.get_text("dict")
    for block in d.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            text = "".join(s.get("text", "") for s in spans).strip()
            if not text:
                continue
            bbox = line.get("bbox")
            if not bbox:
                continue
            x0, y0, x1, y1 = bbox
            out.append({"text": text, "rect": fitz.Rect(x0, y0, x1, y1)})
    out.sort(key=lambda r: (round(r["rect"].y0, 2), round(r["rect"].x0, 2)))
    return out


def page_image_rects(page: fitz.Page) -> list[tuple[int, fitz.Rect]]:
    """[(xref, rect on page), ...]"""
    found = []
    for info in page.get_images(full=True):
        xref = info[0]
        try:
            rects = page.get_image_rects(xref)
        except Exception:
            continue
        for r in rects:
            found.append((xref, r))
    return found


def horiz_overlap(a: fitz.Rect, b: fitz.Rect) -> bool:
    return not (a.x1 < b.x0 or a.x0 > b.x1)


def pick_image_for_sku(sku_rect: fitz.Rect, img_items: list[tuple[int, fitz.Rect]]):
    """选 SKU 上方、同列（水平重叠优先）的图。"""
    best = None
    best_key = None
    for xref, rect in img_items:
        if rect.y1 > sku_rect.y0 + 8:
            continue
        ov = horiz_overlap(rect, sku_rect)
        key = (ov, rect.y1)
        if best_key is None or key > best_key:
            best_key = key
            best = (xref, rect)
    if best:
        return best
    # 退化为 SKU 上方最近的图
    above = [(x, r) for x, r in img_items if r.y1 <= sku_rect.y0 + 5]
    if not above:
        return None
    return max(above, key=lambda t: t[1].y1)


def export_product_image_clip(
    doc: fitz.Document,
    page: fitz.Page,
    sku_rect: fitz.Rect,
    boundaries: list[float],
    col_idx: int,
    dest: Path,
) -> bool:
    """
    以 SKU 行为锚点：在水平方向向左扩展选取嵌入商品图，向上扩展边距；
    裁切底边落在「嵌入图块」下缘附近，不拉到 SKU 线，避免把标题/CASE 等文字裁进图里。
    """
    pw = float(page.rect.width)
    ph = float(page.rect.height)
    cl, cr = boundaries[col_idx], boundaries[col_idx + 1]
    # SKU 锚点：向左扩更多（目录图常在 SKU 左侧）
    band_left = sku_rect.x0 - 88.0
    band_right = sku_rect.x1 + 14.0
    # 本卡片垂直范围：避免并入上一行商品的图
    card_top_limit = sku_rect.y0 - 280.0

    imgs = page_image_rects(page)
    candidates: list[fitz.Rect] = []
    for _xref, r in imgs:
        if r.y1 > sku_rect.y0 - 3:
            continue
        if r.y0 < max(0.0, card_top_limit - 20):
            continue
        if r.x1 < band_left - 2 or r.x0 > band_right + 2:
            continue
        candidates.append(r)

    # 忽略过小的装饰图，优先大块商品图
    def _area(r: fitz.Rect) -> float:
        return max(0.0, r.x1 - r.x0) * max(0.0, r.y1 - r.y0)

    big = [r for r in candidates if _area(r) >= 800]
    if big:
        candidates = big

    left_pad = 16.0
    top_pad = 20.0
    right_pad = 14.0
    bot_pad = 8.0

    if candidates:
        ux0 = min(r.x0 for r in candidates)
        uy0 = min(r.y0 for r in candidates)
        ux1 = max(r.x1 for r in candidates)
        uy1 = max(r.y1 for r in candidates)
        # 向左、向上在图块 union 基础上再扩；底边只到图块下缘+小边距（不含文字区）
        x0 = max(0.0, min(ux0, sku_rect.x0) - left_pad)
        y0 = max(0.0, uy0 - top_pad)
        x1 = min(pw, max(ux1, sku_rect.x1) + right_pad)
        y1 = min(ph, uy1 + bot_pad)
    else:
        # 无嵌入图块时：以 SKU 为中心向左上扩的固定比例框（仍不拉到 SKU 线）
        w = max(96.0, min(200.0, (cr - cl) * 0.92))
        cx = (sku_rect.x0 + sku_rect.x1) / 2 - w * 0.38
        h = min(200.0, sku_rect.y0 - max(0.0, card_top_limit))
        if h < 40:
            h = 120.0
        x0 = max(0.0, cx - left_pad)
        x1 = min(pw, cx + w + right_pad)
        y1 = sku_rect.y0 - 8.0
        y0 = max(0.0, y1 - h - top_pad)

    if y1 - y0 < 28 or x1 - x0 < 36:
        return False
    if y1 > sku_rect.y0 - 2:
        y1 = sku_rect.y0 - 2
    clip = fitz.Rect(x0, y0, x1, y1)
    try:
        mat = fitz.Matrix(2.0, 2.0)
        pix = page.get_pixmap(matrix=mat, clip=clip, alpha=False)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(dest))
        pix = None
        return dest.is_file()
    except Exception:
        return False


def save_image(doc: fitz.Document, xref: int, dest: Path) -> bool:
    try:
        pix = fitz.Pixmap(doc, xref)
        if pix.n - pix.alpha > 3:
            pix = fitz.Pixmap(fitz.csRGB, pix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(dest))
        pix = None
        return dest.is_file()
    except Exception:
        return False


def _png_to_jpg(png_path: Path, jpg_path: Path) -> bool:
    """将临时 PNG 转为 JPEG 并删除 PNG。"""
    try:
        from PIL import Image

        im = Image.open(png_path).convert("RGB")
        jpg_path.parent.mkdir(parents=True, exist_ok=True)
        im.save(jpg_path, "JPEG", quality=88, optimize=True)
        if jpg_path.is_file():
            try:
                png_path.unlink(missing_ok=True)
            except OSError:
                pass
            return True
    except Exception:
        pass
    return False


def export_pdf_product_images_jpg(
    pdf_path: Path,
    media_products: Path | None = None,
) -> tuple[list[str], list[str]]:
    """
    按与 export_images_from_pdf 相同的 SKU 邻域逻辑切图，保存为 {media_products}/{sku}.jpg。
    返回：
      - success_skus：成功写出 jpg 的 SKU（去重、排序）
      - pdf_skus_ordered：PDF 中按阅读顺序出现的 SKU（去重保序），用于对账
    """
    media_products = media_products or MEDIA_PRODUCTS
    doc = fitz.open(str(pdf_path))
    media_products.mkdir(parents=True, exist_ok=True)
    entries, page_imgs, page_width, page_boundaries = _build_entry_index(doc)
    plain = [e["text"] for e in entries]
    sku_indices = [i for i, t in enumerate(plain) if SKU_ANY.search(t)]
    written: set[str] = set()
    pdf_order_unique: list[str] = []
    seen_pdf: set[str] = set()

    for sku_i in sku_indices:
        m = SKU_ANY.search(plain[sku_i])
        if not m:
            continue
        sku = m.group(1)
        if sku not in seen_pdf:
            seen_pdf.add(sku)
            pdf_order_unique.append(sku)
        sku_page = entries[sku_i]["page"]
        sku_rect = entries[sku_i]["rect"]
        pw = page_width.get(sku_page, 600)
        boundaries = page_boundaries.get(sku_page, [0.0, pw + 1.0])
        sku_mid = (sku_rect.x0 + sku_rect.x1) / 2
        col_idx = sku_column_index(sku_mid, boundaries)
        imgs = page_imgs.get(sku_page, [])
        dest_jpg = media_products / f"{sku}.jpg"
        tmp_png = media_products / f".__extract_{sku}.png"
        page_obj = doc[sku_page - 1]
        ok = False
        try:
            if export_product_image_clip(doc, page_obj, sku_rect, boundaries, col_idx, tmp_png):
                ok = _png_to_jpg(tmp_png, dest_jpg)
            else:
                picked = pick_image_for_sku(sku_rect, imgs)
                if picked:
                    xref, _ = picked
                    if save_image(doc, xref, tmp_png):
                        ok = _png_to_jpg(tmp_png, dest_jpg)
        finally:
            try:
                tmp_png.unlink(missing_ok=True)
            except OSError:
                pass
        if ok:
            written.add(sku)
    doc.close()
    return sorted(written), pdf_order_unique


def parse_card_meta(lines: list[str]) -> dict:
    """lines: SKU 行起到 PRICE 行止（含）。"""
    unit = ""
    case = ""
    shelf = 12
    ps, pc = None, None
    for s in lines:
        m = CASE_RE.search(s)
        if m:
            case = m.group(1).strip()
        m = SINGLE_RE.search(s)
        if m:
            unit = m.group(1).strip()
        m = SHELF_RE.search(s.upper().replace("：", ":"))
        if m:
            shelf = int(m.group(1))
        m = PRICE_RE.search(s)
        if m:
            ps, pc = parse_money_pair(m.group(1))
    return {
        "unit_label": unit[:120],
        "case_label": case[:120],
        "shelf_life_months": shelf,
        "price_single": ps,
        "price_case": pc,
    }


def page_column_boundaries(page_width: float, sku_centers: list[float]) -> list[float]:
    """
    按本页所有 SKU 的水平中心聚类为 2～N 列（目录常见 2/3/4 列），
    相邻列中心的中点作为列边界；单行 SKU 则整页一列。
    """
    if not sku_centers:
        return [0.0, page_width + 1.0]
    xs = sorted({round(x, 2) for x in sku_centers})
    if len(xs) == 1:
        return [0.0, page_width + 1.0]
    # 略紧：避免一行里相邻两个商品（如 L020202 与 L020302）被合并成同一「列」
    gap_thresh = max(12.0, page_width * 0.02)
    clusters: list[list[float]] = [[xs[0]]]
    for x in xs[1:]:
        if x - clusters[-1][-1] > gap_thresh:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    centers = [sum(c) / len(c) for c in clusters]
    if len(centers) == 1:
        return [0.0, page_width + 1.0]
    boundaries = [0.0]
    for i in range(len(centers) - 1):
        boundaries.append((centers[i] + centers[i + 1]) / 2)
    boundaries.append(page_width + 1.0)
    return boundaries


def sku_column_index(sku_mid: float, boundaries: list[float]) -> int:
    for i in range(len(boundaries) - 1):
        if boundaries[i] <= sku_mid < boundaries[i + 1]:
            return i
    return max(0, len(boundaries) - 2)


def line_in_column_bounds(
    line_rect: fitz.Rect, boundaries: list[float], col_idx: int
) -> bool:
    cl, cr = boundaries[col_idx], boundaries[col_idx + 1]
    return not (line_rect.x1 < cl - 3 or line_rect.x0 > cr + 3)


def find_price_line_index(
    sku_i: int,
    sku_rect: fitz.Rect,
    sku_page: int,
    boundaries: list[float],
    col_idx: int,
    entries: list[dict],
    plain: list[str],
    max_look: int = 90,
) -> int | None:
    """
    在「同列条带」内，于 SKU 下方 y 窗口内选取水平中心与 SKU 最近的 PRICE 行，
    解决同一网格行上多个 SKU 共用同一聚类列、线性扫描被下一个 SKU 截断的问题。
    """
    sku_mid = (sku_rect.x0 + sku_rect.x1) / 2
    sku_y0 = sku_rect.y0
    y_lo = sku_y0 + 1.5
    y_hi = sku_y0 + 160.0
    candidates: list[tuple[float, int]] = []

    for k in range(sku_i + 1, min(len(plain), sku_i + max_look)):
        if entries[k]["page"] != sku_page:
            break
        r = entries[k]["rect"]
        if not line_in_column_bounds(r, boundaries, col_idx):
            continue
        if not PRICE_RE.search(plain[k]):
            continue
        if r.y0 < y_lo or r.y0 > y_hi:
            continue
        pmid = (r.x0 + r.x1) / 2
        candidates.append((abs(pmid - sku_mid), k))

    if not candidates:
        return None
    candidates.sort(key=lambda t: (t[0], t[1]))
    return candidates[0][1]


def _build_entry_index(doc: fitz.Document):
    """全局行流 + 每页列边界与嵌入图列表（与 extract_catalog 共用）。"""
    entries: list[dict] = []
    page_imgs: dict[int, list[tuple[int, fitz.Rect]]] = {}
    page_width: dict[int, float] = {}
    page_boundaries: dict[int, list[float]] = {}

    for pno in range(len(doc)):
        page = doc[pno]
        page_imgs[pno + 1] = page_image_rects(page)
        pw = float(page.rect.width)
        page_width[pno + 1] = pw
        rows = extract_ordered_lines(page)
        sku_xs = []
        for row in rows:
            if SKU_ANY.search(row["text"]):
                sku_xs.append((row["rect"].x0 + row["rect"].x1) / 2)
        page_boundaries[pno + 1] = page_column_boundaries(pw, sku_xs)
        for row in rows:
            t = row["text"]
            entries.append(
                {
                    "text": t,
                    "rect": row["rect"],
                    "page": pno + 1,
                }
            )
    return entries, page_imgs, page_width, page_boundaries


def export_images_from_pdf(pdf_path: Path) -> tuple[int, int]:
    """
    仅按 SKU 锚点重导 media/products/{sku}.png，不解析价格、不写 CSV、不导入数据库。
    返回 (成功写入文件数, 处理的 SKU 出现次数)。
    """
    doc = fitz.open(str(pdf_path))
    MEDIA_PRODUCTS.mkdir(parents=True, exist_ok=True)
    entries, page_imgs, page_width, page_boundaries = _build_entry_index(doc)
    plain = [e["text"] for e in entries]
    sku_indices = [i for i, t in enumerate(plain) if SKU_ANY.search(t)]
    ok_skus: set[str] = set()
    n_seen = 0
    for sku_i in sku_indices:
        m = SKU_ANY.search(plain[sku_i])
        if not m:
            continue
        sku = m.group(1)
        n_seen += 1
        sku_page = entries[sku_i]["page"]
        sku_rect = entries[sku_i]["rect"]
        pw = page_width.get(sku_page, 600)
        boundaries = page_boundaries.get(sku_page, [0.0, pw + 1.0])
        sku_mid = (sku_rect.x0 + sku_rect.x1) / 2
        col_idx = sku_column_index(sku_mid, boundaries)
        imgs = page_imgs.get(sku_page, [])
        dest = MEDIA_PRODUCTS / f"{sku}.png"
        page_obj = doc[sku_page - 1]
        ok = False
        if export_product_image_clip(doc, page_obj, sku_rect, boundaries, col_idx, dest):
            ok = True
        else:
            picked = pick_image_for_sku(sku_rect, imgs)
            if picked:
                xref, _ = picked
                ok = save_image(doc, xref, dest)
        if ok:
            ok_skus.add(sku)
    doc.close()
    return len(ok_skus), n_seen


def extract_catalog(pdf_path: Path, skip_images: bool = False):
    doc = fitz.open(str(pdf_path))
    if not skip_images:
        MEDIA_PRODUCTS.mkdir(parents=True, exist_ok=True)

    entries, page_imgs, page_width, page_boundaries = _build_entry_index(doc)

    plain = [e["text"] for e in entries]
    sku_indices = [i for i, t in enumerate(plain) if SKU_ANY.search(t)]

    current_cat = "默认分类"
    prev_price_end = -1
    cards: list[dict] = []
    failures: list[tuple[str, str, int, str]] = []  # sku, page, line, reason

    for si, sku_i in enumerate(sku_indices):
        m = SKU_ANY.search(plain[sku_i])
        if not m:
            continue
        sku = m.group(1)
        sku_page = entries[sku_i]["page"]
        sku_rect = entries[sku_i]["rect"]
        pw = page_width.get(sku_page, 600)
        boundaries = page_boundaries.get(sku_page, [0.0, pw + 1.0])
        sku_mid = (sku_rect.x0 + sku_rect.x1) / 2
        col_idx = sku_column_index(sku_mid, boundaries)

        price_i = find_price_line_index(
            sku_i, sku_rect, sku_page, boundaries, col_idx, entries, plain
        )

        if price_i is None:
            failures.append((sku, str(sku_page), sku_i + 1, "同栏卡片内无 PRICE"))
            prev_price_end = sku_i
            continue

        meta_lines = plain[sku_i : price_i + 1]
        meta = parse_card_meta(meta_lines)
        if meta["price_single"] is None:
            failures.append((sku, str(sku_page), sku_i + 1, "PRICE 无法解析数字"))
            prev_price_end = sku_i
            continue

        ps, pc = meta["price_single"], meta["price_case"]
        if pc is None:
            pc = ps

        # 名称：上一 PRICE 之后到本 SKU 之前；同卡片条带 + 通栏分类标题
        name_parts: list[str] = []
        for j in range(prev_price_end + 1, sku_i):
            line = plain[j]
            ej = entries[j]
            if ej["page"] != sku_page:
                break
            if is_section_header_line(line):
                current_cat = re.sub(r"\s+", " ", line.strip())[:100]
                continue
            if not line.strip():
                continue
            if is_meta_line(line) and not SKU_ANY.search(line):
                continue
            if SKU_ANY.search(line) and j != sku_i:
                continue
            mid = (ej["rect"].x0 + ej["rect"].x1) / 2
            fullwidth_title = abs(mid - pw / 2) < pw * 0.08
            if not line_in_column_bounds(ej["rect"], boundaries, col_idx) and not fullwidth_title:
                continue
            name_parts.append(line)
        name = " ".join(re.sub(r"\s+", " ", p).strip() for p in name_parts if p.strip())
        if not name:
            name = sku

        # 图片：同页列条带整页渲染裁切；失败则退回嵌入图（skip_images 时跳过，仅用于价格校对）
        img_path_rel = ""
        if not skip_images:
            imgs = page_imgs.get(sku_page, [])
            dest = MEDIA_PRODUCTS / f"{sku}.png"
            page_obj = doc[sku_page - 1]
            if export_product_image_clip(doc, page_obj, sku_rect, boundaries, col_idx, dest):
                img_path_rel = f"products/{sku}.png"
            else:
                picked = pick_image_for_sku(sku_rect, imgs)
                if picked:
                    xref, _ = picked
                    if save_image(doc, xref, dest):
                        img_path_rel = f"products/{sku}.png"

        cards.append(
            {
                "category": current_cat or "默认分类",
                "name": name[:200],
                "sku": sku,
                "unit_label": meta["unit_label"],
                "case_label": meta["case_label"],
                "price_single": ps,
                "price_case": pc,
                "shelf_life_months": meta["shelf_life_months"],
                "image": img_path_rel,
                "page": sku_page,
            }
        )
        prev_price_end = price_i

    doc.close()
    return cards, failures


def dedupe_cards(cards: list[dict]) -> list[dict]:
    by_sku: dict[str, dict] = {}
    for c in cards:
        by_sku[c["sku"]] = c
    return list(by_sku.values())


def main():
    argv = [a for a in sys.argv[1:] if a not in ("--images-only", "--csv-only")]
    images_only = "--images-only" in sys.argv[1:]
    pdf_path = Path(argv[0]) if argv else PDF_DEFAULT
    if not pdf_path.is_file():
        print(f"找不到 PDF: {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"PDF: {pdf_path}")
    if images_only:
        n_ok, n_seen = export_images_from_pdf(pdf_path)
        print(
            f"\n—— 仅重导图片 ——\n成功 SKU 数: {n_ok}（PDF 内 SKU 行数 {n_seen}，重复 SKU 取最后一次）\n目录: {MEDIA_PRODUCTS}"
        )
        return

    cards, failures = extract_catalog(pdf_path)
    final = dedupe_cards(cards)

    # 统计
    with_image = sum(1 for c in final if c.get("image"))
    with_price = sum(1 for c in final if c.get("price_single") is not None)

    with OUT_CSV.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HEADER)
        w.writeheader()
        for c in sorted(final, key=lambda x: x["sku"]):
            case_lbl = c.get("case_label") or ""
            w.writerow(
                {
                    "category": c["category"],
                    "name": c["name"],
                    "sku": c["sku"],
                    "unit_label": c["unit_label"],
                    "case_label": case_lbl,
                    "price_single": c["price_single"],
                    "price_case": c["price_case"],
                    "cost_price_single": 0,
                    "cost_price_case": 0,
                    "shelf_life_months": c.get("shelf_life_months") or 12,
                    "can_split_sale": 1,
                    "minimum_order_qty": 1,
                    "is_active": 1,
                    "stock_quantity": 0,
                    "units_per_case": parse_units_per_case(case_lbl),
                    "image": c.get("image") or "",
                }
            )

    with FAILURES.open("w", encoding="utf-8") as ff:
        ff.write("sku\tpage\tline\terror\n")
        for sku, pg, ln, err in failures:
            ff.write(f"{sku}\t{pg}\t{ln}\t{err}\n")

    print("\n—— 提取统计 ——")
    print(f"成功解析商品（有明确 PRICE）: {len(final)}")
    print(f"成功导出商品图片数: {with_image}")
    print(f"成功写入价格数: {with_price}")
    print(f"解析失败条数（无 PRICE 等）: {len(failures)}")
    print(f"CSV: {OUT_CSV}")
    print(f"失败列表: {FAILURES}")

    if "--csv-only" in sys.argv[1:]:
        return

    import subprocess

    r = subprocess.run(
        [sys.executable, str(ROOT / "manage.py"), "import_products_ready"],
        cwd=str(ROOT),
    )
    sys.exit(r.returncode)


if __name__ == "__main__":
    main()
