"""
Sync official product images from mochaboba.com into Product.official_image_url.

Matching priority:
1) SKU exact match (if mochaboba provides variant SKUs; often empty)
2) Name fuzzy match (normalized tokens + difflib)

Outputs:
- data/mochaboba_image_map.json  (our SKU -> official image URL)
- data/mochaboba_image_report.json (matched / unmatched / candidates)

Usage:
  python manage.py sync_mochaboba_images --limit 500 --min-score 0.80 --write-db
"""

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from django.conf import settings
from django.core.management.base import BaseCommand

from tea_supply.models import Product


def _fetch(url: str) -> str:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="ignore")


def _norm(s: str) -> str:
    s = (s or "").strip().lower()
    s = s.replace("mo'cha", "mocha").replace("mo·cha", "mocha").replace("mocha", "mocha")
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # remove common packaging words that hurt matching
    stop = {
        "default",
        "title",
        "retail",
        "price",
        "pack",
        "size",
        "oz",
        "lb",
        "lbs",
        "case",
        "bag",
        "cups",
        "roll",
        "machine",
        "cm",
        "v",
    }
    toks = [t for t in s.split(" ") if t and t not in stop]
    return " ".join(toks)


def _score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class MochaItem:
    handle: str
    title: str
    norm_title: str
    images: List[str]
    skus: List[str]


def _collect_handles(limit: int) -> List[str]:
    # mochaboba collection is paginated. We fetch until we have enough handles.
    handles: List[str] = []
    page = 1
    seen = set()
    while len(handles) < limit and page <= 40:
        html = _fetch(f"https://mochaboba.com/collections/all?page={page}")
        hrefs = re.findall(r'href="(/collections/all/products/[^"]+)"', html)
        before = len(handles)
        for h in hrefs:
            handle = h.split("/")[-1]
            if handle and handle not in seen:
                seen.add(handle)
                handles.append(handle)
                if len(handles) >= limit:
                    break
        if len(handles) == before:
            break
        page += 1
    return handles


def _fetch_product_json(handle: str) -> Optional[MochaItem]:
    try:
        raw = _fetch(f"https://mochaboba.com/products/{handle}.json")
        data = json.loads(raw)
        prod = data.get("product") or {}
        title = prod.get("title") or handle
        images = [img.get("src") for img in (prod.get("images") or []) if img.get("src")]
        skus = [v.get("sku") for v in (prod.get("variants") or []) if v.get("sku")]
        return MochaItem(
            handle=handle,
            title=title,
            norm_title=_norm(title),
            images=images,
            skus=skus,
        )
    except Exception:
        return None


def _pick_best_image(urls: List[str]) -> str:
    # heuristic: prefer "files/" cdn URLs; otherwise first image
    if not urls:
        return ""
    for u in urls:
        if "cdn.shopify.com" in u:
            return u
    return urls[0]


class Command(BaseCommand):
    help = "Sync mochaboba.com official images into Product.official_image_url"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=500)
        parser.add_argument("--min-score", type=float, default=0.80)
        parser.add_argument("--write-db", action="store_true")

    def handle(self, *args, **options):
        limit = int(options["limit"])
        min_score = float(options["min_score"])
        write_db = bool(options["write_db"])

        out_dir = Path(settings.BASE_DIR) / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        map_path = out_dir / "mochaboba_image_map.json"
        report_path = out_dir / "mochaboba_image_report.json"

        handles = _collect_handles(limit=limit)
        self.stdout.write(self.style.SUCCESS(f"Fetched handles: {len(handles)}"))

        items: List[MochaItem] = []
        sku_to_item: Dict[str, MochaItem] = {}
        for h in handles:
            it = _fetch_product_json(h)
            if not it:
                continue
            if not it.images:
                continue
            items.append(it)
            for s in it.skus:
                sku_to_item[s.strip().upper()] = it

        self.stdout.write(self.style.SUCCESS(f"Fetched product.json with images: {len(items)}"))

        matched: Dict[str, str] = {}
        matched_rows = []
        unmatched = []

        for p in Product.objects.all().order_by("sku"):
            sku = (p.sku or "").strip().upper()
            name = (p.name or "").strip()
            norm_name = _norm(name)

            best: Tuple[float, Optional[MochaItem]] = (0.0, None)

            # 1) SKU match (rare)
            if sku and sku in sku_to_item:
                best = (1.0, sku_to_item[sku])
            else:
                # 2) fuzzy name match
                for it in items:
                    sc = _score(norm_name, it.norm_title)
                    if sc > best[0]:
                        best = (sc, it)

            sc, it = best
            if it and sc >= min_score:
                url = _pick_best_image(it.images)
                if url:
                    matched[sku] = url
                    matched_rows.append(
                        {
                            "sku": sku,
                            "name": name,
                            "score": round(float(sc), 4),
                            "mochaboba_title": it.title,
                            "mochaboba_handle": it.handle,
                            "image_url": url,
                        }
                    )
                    if write_db:
                        Product.objects.filter(pk=p.pk).update(official_image_url=url)
                else:
                    unmatched.append({"sku": sku, "name": name, "reason": "no images"})
            else:
                unmatched.append(
                    {
                        "sku": sku,
                        "name": name,
                        "score": round(float(sc), 4),
                        "best_title": it.title if it else "",
                        "best_handle": it.handle if it else "",
                    }
                )

        map_path.write_text(json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {
            "min_score": min_score,
            "write_db": write_db,
            "matched_count": len(matched_rows),
            "unmatched_count": len(unmatched),
            "matched": matched_rows,
            "unmatched": unmatched,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        self.stdout.write(self.style.SUCCESS(f"Matched: {len(matched_rows)}"))
        self.stdout.write(self.style.WARNING(f"Unmatched: {len(unmatched)}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote map: {map_path}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote report: {report_path}"))

