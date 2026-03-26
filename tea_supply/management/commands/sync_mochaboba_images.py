"""
Sync official product images from mochaboba.com into Product.official_image_url.

Matching priority:
1) SKU exact match (if mochaboba provides variant SKUs; often empty)
2) Name fuzzy match (normalized tokens + difflib)

Outputs:
- data/mochaboba_image_map.json  (our SKU -> official image URL)
- data/mochaboba_image_report.json (matched / unmatched / candidates)
- data/mochaboba_image_candidates.json (all candidates; does NOT write DB by itself)
- data/mochaboba_image_autobind_report.json (what was actually written to DB)

Usage:
  python manage.py sync_mochaboba_images --limit 500 --min-score 0.80 --write-db
"""

import json
import re
import unicodedata
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


KEYWORDS = (
    "boba",
    "syrup",
    "tea",
    "powder",
    "cup",
    "lid",
    "straw",
    "tapioca",
    "jelly",
    "pudding",
    "fructose",
    "sugar",
    "matcha",
    "oolong",
    "jasmine",
    "thai",
    "taro",
    "coconut",
    "mango",
    "peach",
    "lychee",
    "honey",
    "milk",
    "creamer",
)

PRIMARY_KW = ("boba", "syrup", "tea", "powder", "cup", "lid")

KW_GROUPS: Dict[str, str] = {
    # ingredients
    "boba": "ingredient",
    "tapioca": "ingredient",
    "jelly": "ingredient",
    "pudding": "ingredient",
    "syrup": "ingredient",
    "tea": "ingredient",
    "powder": "ingredient",
    "fructose": "ingredient",
    "sugar": "ingredient",
    "milk": "ingredient",
    "creamer": "ingredient",
    "matcha": "ingredient",
    "oolong": "ingredient",
    "jasmine": "ingredient",
    "thai": "ingredient",
    "taro": "ingredient",
    "coconut": "ingredient",
    "mango": "ingredient",
    "peach": "ingredient",
    "lychee": "ingredient",
    "honey": "ingredient",
    # packaging
    "cup": "packaging",
    "lid": "packaging",
    "straw": "packaging",
}


def _norm_base(s: str) -> str:
    # Keep ASCII letters/digits + CJK; normalize to reduce punctuation variants.
    s = unicodedata.normalize("NFKC", (s or ""))
    s = s.strip().lower()
    s = s.replace("mo'cha", "mocha").replace("mo·cha", "mocha").replace("mocha", "mocha")
    # Replace separators with spaces, keep CJK as-is.
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm(s: str) -> str:
    s = _norm_base(s)
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


def _norm_nospace(s: str) -> str:
    # stronger normalization: remove ALL spaces to support mixed-language substring matching
    return _norm(s).replace(" ", "")


def _extract_keywords(s: str) -> List[str]:
    s0 = _norm_base(s)
    if not s0:
        return []
    found = []
    for k in KEYWORDS:
        if k in s0:
            found.append(k)
    return found


def _score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _kw_groups(kws: List[str]) -> List[str]:
    out = []
    seen = set()
    for k in kws:
        g = KW_GROUPS.get(k)
        if g and g not in seen:
            seen.add(g)
            out.append(g)
    return out


def _is_cross_category(local_kws: List[str], remote_kws: List[str]) -> bool:
    lg = set(_kw_groups(local_kws))
    rg = set(_kw_groups(remote_kws))
    if not lg or not rg:
        return False
    return lg.isdisjoint(rg)


def _has_strong_primary_kw_overlap(local_kws: List[str], remote_kws: List[str]) -> bool:
    lk = set(k for k in local_kws if k in PRIMARY_KW)
    rk = set(k for k in remote_kws if k in PRIMARY_KW)
    return bool(lk.intersection(rk))


def _match_score(
    local_name: str,
    remote_title: str,
    *,
    local_kw: List[str],
    remote_kw: List[str],
) -> float:
    """
    Aggressive fuzzy match:
    - ignore case, punctuation, repeated whitespace
    - also compare "no-space" versions for mixed CN/EN and compact strings
    - boost if shared domain keywords are present (boba/tea/cup/...)
    - reward substring containment (common in Shopify titles)
    """
    a = _norm(local_name)
    b = _norm(remote_title)
    a2 = a.replace(" ", "")
    b2 = b.replace(" ", "")
    if not a2 or not b2:
        return 0.0

    base = max(_score(a, b), _score(a2, b2))

    # substring containment signal (after no-space normalization)
    if len(a2) >= 6 and (a2 in b2 or b2 in a2):
        base = max(base, 0.93)

    # keyword overlap bonus (cap to avoid overpowering)
    if local_kw and remote_kw:
        shared = len(set(local_kw).intersection(remote_kw))
        if shared:
            base += min(0.18, 0.06 * shared)

    # clamp
    if base < 0.0:
        return 0.0
    if base > 1.0:
        return 1.0
    return base

@dataclass
class MochaItem:
    handle: str
    title: str
    norm_title: str
    norm_title_nospace: str
    images: List[str]
    skus: List[str]
    keywords: List[str]


def _collect_handles(limit: int) -> List[str]:
    # mochaboba collection is paginated. We fetch until we have enough handles.
    handles: List[str] = []
    page = 1
    seen = set()
    while len(handles) < limit and page <= 40:
        html = _fetch(f"https://mochaboba.com/collections/all?page={page}")
        hrefs = []
        # Shopify themes vary: sometimes links are /collections/all/products/<handle>,
        # sometimes just /products/<handle>.
        hrefs += re.findall(r'href="(/collections/all/products/[^"]+)"', html)
        hrefs += re.findall(r'href="(/products/[^"?]+)"', html)
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
            norm_title_nospace=_norm_nospace(title),
            images=images,
            skus=skus,
            keywords=_extract_keywords(title),
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
        parser.add_argument(
            "--min-score",
            type=float,
            default=0.68,
            help="Minimum score for including a candidate in candidates file (not DB threshold).",
        )
        parser.add_argument(
            "--write-db",
            action="store_true",
            help="Apply strict autobind rules and write only high-confidence matches to DB.",
        )
        parser.add_argument(
            "--overwrite",
            action="store_true",
            help="Allow overwrite ONLY when SKU exact match (otherwise ignored).",
        )

    def handle(self, *args, **options):
        limit = int(options["limit"])
        min_score = float(options["min_score"])
        write_db = bool(options["write_db"])
        overwrite = bool(options["overwrite"])

        out_dir = Path(settings.BASE_DIR) / "data"
        out_dir.mkdir(parents=True, exist_ok=True)
        map_path = out_dir / "mochaboba_image_map.json"
        report_path = out_dir / "mochaboba_image_report.json"
        candidates_path = out_dir / "mochaboba_image_candidates.json"
        autobind_path = out_dir / "mochaboba_image_autobind_report.json"

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
        db_updates = 0
        candidates_all = []
        blocked_low_conf = []
        autobound_rows = []

        # Index items by keyword to reduce false matches and speed up.
        kw_to_items: Dict[str, List[MochaItem]] = {}
        for it in items:
            for k in it.keywords:
                kw_to_items.setdefault(k, []).append(it)

        for p in Product.objects.all().order_by("sku"):
            sku = (p.sku or "").strip().upper()
            name = (p.name or "").strip()
            norm_name = _norm(name)
            norm_name_nospace = _norm_nospace(name)
            local_kw = _extract_keywords(name)
            current_official = (p.official_image_url or "").strip()

            best: Tuple[float, Optional[MochaItem]] = (0.0, None)
            second_best = 0.0
            evidence = "name"

            # 1) SKU match (rare)
            if sku and sku in sku_to_item:
                best = (1.0, sku_to_item[sku])
                evidence = "sku"
            else:
                # 2) fuzzy name match
                if local_kw:
                    cand: List[MochaItem] = []
                    seen = set()
                    for k in local_kw:
                        for it in kw_to_items.get(k, []):
                            if it.handle not in seen:
                                seen.add(it.handle)
                                cand.append(it)
                    candidates = cand or items
                else:
                    candidates = items

                for it in candidates:
                    sc = _match_score(
                        name,
                        it.title,
                        local_kw=local_kw,
                        remote_kw=it.keywords,
                    )
                    # extra small bump for exact no-space equality
                    if norm_name_nospace and norm_name_nospace == it.norm_title_nospace:
                        sc = max(sc, 0.98)

                    if sc > best[0]:
                        second_best = best[0]
                        best = (sc, it)
                    elif sc > second_best:
                        second_best = sc

            sc, it = best
            # protect against ambiguous matches when lowering threshold
            is_ambiguous = (sc - second_best) < 0.03 and sc < 0.97
            url = _pick_best_image(it.images) if it else ""
            cross_cat = _is_cross_category(local_kw, it.keywords) if it else False
            strong_kw = _has_strong_primary_kw_overlap(local_kw, it.keywords) if it else False

            allow_autobind = False
            reason = ""
            if it and url:
                if evidence == "sku":
                    allow_autobind = True
                    reason = "SKU exact match"
                elif (not cross_cat) and sc >= 0.92 and (not is_ambiguous):
                    allow_autobind = True
                    reason = "High name similarity (>=0.92)"
                elif (not cross_cat) and strong_kw and sc >= 0.95 and (not is_ambiguous):
                    allow_autobind = True
                    reason = "Strong primary keyword match + very high score (>=0.95)"
                else:
                    allow_autobind = False
                    if cross_cat:
                        reason = "Blocked: cross-category keyword signals"
                    elif is_ambiguous:
                        reason = "Blocked: ambiguous (top1 too close to top2)"
                    else:
                        reason = "Blocked: below strict autobind thresholds"
            else:
                if not it:
                    reason = "No candidate found"
                elif not url:
                    reason = "Candidate has no usable image URL"

            # Step 1: generate candidates for ALL currently-unmatched products.
            if not current_official:
                candidates_all.append(
                    {
                        "sku": sku,
                        "name": name,
                        "candidate_mochaboba_title": it.title if it else "",
                        "candidate_mochaboba_handle": it.handle if it else "",
                        "candidate_image_url": url,
                        "score": round(float(sc), 4),
                        "second_best": round(float(second_best), 4),
                        "evidence": evidence if it else "",
                        "local_keywords": local_kw,
                        "remote_keywords": it.keywords if it else [],
                        "cross_category": bool(cross_cat),
                        "strong_primary_keyword": bool(strong_kw),
                        "autobind_allowed": bool(allow_autobind),
                        "autobind_reason": reason,
                        "blocked_due_to_min_score": bool(it and sc < min_score),
                    }
                )

            # For mapping/report, keep the old behavior to reduce noise.
            if it and sc >= min_score:
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
                            "keywords": local_kw,
                            "second_best": round(float(second_best), 4),
                        }
                    )
                    if write_db and allow_autobind:
                        should_write = False
                        if not current_official:
                            should_write = True
                        else:
                            # Do not overwrite existing bindings unless SKU-exact match.
                            if evidence == "sku" and overwrite and current_official != url:
                                should_write = True
                        if should_write:
                            Product.objects.filter(pk=p.pk).update(official_image_url=url)
                            db_updates += 1
                            autobound_rows.append(
                                {
                                    "sku": sku,
                                    "name": name,
                                    "image_url": url,
                                    "score": round(float(sc), 4),
                                    "evidence": evidence,
                                    "reason": reason,
                                    "overwrote": bool(current_official),
                                }
                            )
                        else:
                            if not current_official and (not allow_autobind):
                                blocked_low_conf.append(
                                    {
                                        "sku": sku,
                                        "name": name,
                                        "score": round(float(sc), 4),
                                        "reason": reason,
                                    }
                                )
                else:
                    unmatched.append({"sku": sku, "name": name, "reason": "no images"})
            else:
                unmatched.append(
                    {
                        "sku": sku,
                        "name": name,
                        "score": round(float(sc), 4),
                        "second_best": round(float(second_best), 4),
                        "best_title": it.title if it else "",
                        "best_handle": it.handle if it else "",
                        "keywords": local_kw,
                    }
                )

        map_path.write_text(json.dumps(matched, ensure_ascii=False, indent=2), encoding="utf-8")
        report = {
            "min_score": min_score,
            "write_db": write_db,
            "overwrite": overwrite,
            "matched_count": len(matched_rows),
            "unmatched_count": len(unmatched),
            "db_updates": db_updates,
            "matched": matched_rows,
            "unmatched": unmatched,
        }
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        candidates_payload = {
            "min_score": min_score,
            "strict_autobind_rules": {
                "sku_exact": True,
                "name_similarity_threshold": 0.92,
                "primary_keyword_threshold": 0.95,
                "primary_keywords": list(PRIMARY_KW),
                "blocked_if_cross_category": True,
                "blocked_if_ambiguous": True,
            },
            "candidates_count": len(candidates_all),
            "candidates": candidates_all,
        }
        candidates_path.write_text(
            json.dumps(candidates_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        autobind_payload = {
            "write_db": write_db,
            "db_updates": db_updates,
            "autobound_count": len(autobound_rows),
            "blocked_low_conf_count": len(blocked_low_conf),
            "autobound": autobound_rows,
            "blocked_low_conf_examples": blocked_low_conf[:200],
        }
        autobind_path.write_text(
            json.dumps(autobind_payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        self.stdout.write(self.style.SUCCESS(f"Matched: {len(matched_rows)}"))
        if write_db:
            self.stdout.write(self.style.SUCCESS(f"DB updated rows: {db_updates}"))
        self.stdout.write(self.style.WARNING(f"Unmatched: {len(unmatched)}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote map: {map_path}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote report: {report_path}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote candidates: {candidates_path}"))
        self.stdout.write(self.style.SUCCESS(f"Wrote autobind report: {autobind_path}"))

