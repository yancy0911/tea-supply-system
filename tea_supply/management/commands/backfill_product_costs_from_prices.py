from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand

from tea_supply.models import Product


class Command(BaseCommand):
    help = "按售价回填缺失成本价（仅 cost<=0 且 price>0 时，成本=售价*0.6）"

    def handle(self, *args, **options):
        scanned_count = 0
        updated_count = 0
        filled_single_count = 0
        filled_case_count = 0
        updated_skus = []
        ratio = Decimal("0.6")
        q = Decimal("0.01")

        for p in Product.objects.all().order_by("id"):
            scanned_count += 1
            changed = False
            update_fields = []

            ps = Decimal(str(p.price_single or 0))
            pcs = Decimal(str(p.cost_price_single or 0))
            if ps > 0 and pcs <= 0:
                p.cost_price_single = float((ps * ratio).quantize(q, rounding=ROUND_HALF_UP))
                changed = True
                update_fields.append("cost_price_single")
                filled_single_count += 1

            pc = Decimal(str(p.price_case or 0))
            pcc = Decimal(str(p.cost_price_case or 0))
            if pc > 0 and pcc <= 0:
                p.cost_price_case = float((pc * ratio).quantize(q, rounding=ROUND_HALF_UP))
                changed = True
                update_fields.append("cost_price_case")
                filled_case_count += 1

            if changed:
                p.save(update_fields=update_fields)
                updated_count += 1
                if len(updated_skus) < 20:
                    updated_skus.append(p.sku)

        self.stdout.write(self.style.SUCCESS(f"scanned_count={scanned_count}"))
        self.stdout.write(self.style.SUCCESS(f"updated_count={updated_count}"))
        self.stdout.write(self.style.SUCCESS(f"filled_single_count={filled_single_count}"))
        self.stdout.write(self.style.SUCCESS(f"filled_case_count={filled_case_count}"))
        self.stdout.write(f"updated_skus_top20={updated_skus}")
