"""
Standard English names for product categories (DB + imports).

Use normalize_category_name_to_english() for any raw label; use
normalize_all_product_categories_in_db() for one-shot DB cleanup (merges duplicates).
"""
from __future__ import annotations

import re
from collections import defaultdict

from django.db import transaction

# Longer / more specific phrases first.
CATEGORY_KEYWORD_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("雪克杯", "Shaker Cup"),
    ("雪克", "Shaker Cup"),
    ("木薯波巴", "Tapioca Boba"),
    ("果浆/配料", "Pulp & toppings"),
    ("默认分类", "Default"),
    ("未分类", "Default"),
    ("全部分类", "All Categories"),
    ("爆爆珠", "Popping Boba"),
    ("椰果", "Coconut Jelly"),
    ("果酱", "Fruit Jam"),
    ("特殊粉", "Special Powder"),
    ("糖浆", "Syrup"),
    ("粉类", "Powders"),
    ("小料", "Boba & toppings"),
    ("罐头辅料", "Canned toppings"),
    ("包材/器具", "Packaging & tools"),
    ("工具", "Tools"),
    ("机器", "Machinery"),
    ("果肉", "Pulp Topping"),
    ("纯粉", "Pure Powder"),
    ("茶包", "Tea Bag"),
    ("茶叶", "Tea"),
    ("奶制品", "Dairy & creamer"),
)

CJK_RE = re.compile(r"[\u4e00-\u9fff]+")

# 整箱 before 箱; used for product unit fields (admin / optional product pass).
LABEL_PHRASE_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("整箱", "Case"),
    ("单品", "Single"),
    ("袋", "Bag"),
    ("箱", "Case"),
)


def _collapse_spaces(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _dedupe_consecutive_words(s: str) -> str:
    parts = s.split()
    if not parts:
        return ""
    out = [parts[0]]
    for p in parts[1:]:
        if p.lower() != out[-1].lower():
            out.append(p)
    return " ".join(out)


def normalize_category_name_to_english(raw: str) -> str:
    """Map known Chinese phrases to English, strip leftover CJK, collapse spaces."""
    s = raw or ""
    for zh, en in CATEGORY_KEYWORD_REPLACEMENTS:
        s = s.replace(zh, f" {en} ")
    s = CJK_RE.sub("", s)
    s = _collapse_spaces(s)
    s = _dedupe_consecutive_words(s)
    return s


def normalize_product_field_to_english(raw: str, *, apply_label_phrases: bool) -> str:
    """Strip CJK from product text fields; optionally map 单品/整箱/袋/箱."""
    s = raw or ""
    if apply_label_phrases:
        for zh, en in LABEL_PHRASE_REPLACEMENTS:
            s = s.replace(zh, en)
    s = CJK_RE.sub("", s)
    return _collapse_spaces(s)


def normalize_all_product_categories_in_db(*, dry_run: bool = False) -> dict:
    """
    Rename every ProductCategory to English, merge rows that map to the same name.

    - Reassign Product.category to the kept row (lowest id) before deleting duplicates.
    - Empty result after normalization becomes \"Default\".
    """
    from tea_supply.models import Product, ProductCategory

    stats: dict = {"categories_renamed": 0, "categories_deleted": 0, "errors": []}

    cats = list(ProductCategory.objects.order_by("id"))
    if not cats:
        return stats

    id_target: dict[int, str] = {}
    for c in cats:
        t = normalize_category_name_to_english(c.name or "")
        if not t:
            t = "Default"
        id_target[c.id] = t

    target_to_ids: dict[str, list[int]] = defaultdict(list)
    for c in cats:
        target_to_ids[id_target[c.id]].append(c.id)

    def preview() -> dict:
        out = {
            "categories_renamed": 0,
            "categories_deleted": 0,
            "errors": [],
            "dry_run": True,
        }
        for target, ids in target_to_ids.items():
            ids_sorted = sorted(ids)
            if len(ids_sorted) > 1:
                out["categories_deleted"] += len(ids_sorted) - 1
            keeper_id = ids_sorted[0]
            obj = next((c for c in cats if c.id == keeper_id), None)
            if obj and obj.name != target:
                out["categories_renamed"] += 1
        return out

    if dry_run:
        return preview()

    def run_mutations():
        for target, ids in target_to_ids.items():
            ids_sorted = sorted(ids)
            keeper = ids_sorted[0]
            for vid in ids_sorted[1:]:
                Product.objects.filter(category_id=vid).update(category_id=keeper)
                ProductCategory.objects.filter(pk=vid).delete()
                stats["categories_deleted"] += 1
            obj = ProductCategory.objects.filter(pk=keeper).first()
            if not obj:
                stats["errors"].append(f"Missing keeper pk={keeper} for target={target!r}")
                continue
            if obj.name != target:
                obj.name = target
                obj.save(update_fields=["name"])
                stats["categories_renamed"] += 1

    with transaction.atomic():
        run_mutations()

    return stats
