from django.core.management.base import BaseCommand

from tea_supply.category_names import normalize_all_product_categories_in_db


class Command(BaseCommand):
    help = (
        "One-shot: normalize all ProductCategory.name to English, merge duplicates "
        "(same canonical name), reassign products to the kept category row."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print planned renames/merges without writing the database.",
        )

    def handle(self, *args, **opts):
        dry = bool(opts["dry_run"])
        stats = normalize_all_product_categories_in_db(dry_run=dry)
        self.stdout.write(str(stats))
        if dry:
            self.stdout.write(self.style.WARNING("dry-run: no changes saved"))
        else:
            self.stdout.write(self.style.SUCCESS("Done."))
