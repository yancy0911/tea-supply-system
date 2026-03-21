"""
根据 MEDIA_ROOT 下已有文件，为 image 为空的商品自动补全路径。
用法: python manage.py backfill_product_images
"""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from tea_supply.models import Product


class Command(BaseCommand):
    help = "扫描媒体目录，按 SKU 匹配文件名并写入 Product.image"

    def handle(self, *args, **options):
        root = Path(settings.MEDIA_ROOT)
        if not root.exists():
            self.stdout.write(self.style.WARNING(f"MEDIA_ROOT 不存在: {root}，跳过。"))
            self._report_missing()
            return

        by_stem = {}
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
                continue
            rel = path.relative_to(root)
            rel_s = str(rel).replace("\\", "/")
            by_stem.setdefault(path.stem, rel_s)

        updated = 0
        for p in Product.objects.all().order_by("sku"):
            img = (p.image or "").strip()
            if img:
                if (root / img).is_file():
                    continue
                self.stdout.write(self.style.WARNING(f"[损坏路径] {p.sku} -> {img}（文件不存在）"))

            cand = None
            if p.sku in by_stem:
                cand = by_stem[p.sku]
            if not cand:
                for ext in (".png", ".jpg", ".jpeg", ".webp"):
                    trial = root / "products" / f"{p.sku}{ext}"
                    if trial.is_file():
                        cand = f"products/{p.sku}{ext}"
                        break
            if cand:
                p.image = cand
                p.save(update_fields=["image"])
                updated += 1
                self.stdout.write(self.style.SUCCESS(f"[已补全] {p.sku} -> {cand}"))
            else:
                self.stdout.write(f"[仍缺图] {p.sku} | {p.name}")

        self.stdout.write(self.style.SUCCESS(f"完成：更新 {updated} 条。"))
        self._report_missing()

    def _report_missing(self):
        root = Path(settings.MEDIA_ROOT)
        missing = []
        for p in Product.objects.all().order_by("sku"):
            img = (p.image or "").strip()
            if not img or not (root / img).is_file():
                missing.append(p.sku)
        if missing:
            self.stdout.write(self.style.WARNING("仍无有效图片文件的 SKU：" + ", ".join(missing)))
        else:
            self.stdout.write(self.style.SUCCESS("所有商品均有有效图片路径。"))
