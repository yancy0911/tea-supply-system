# Generated manually: data migration — English category names + merge duplicates.

from django.db import migrations


def forwards(apps, schema_editor):
    from tea_supply.category_names import normalize_all_product_categories_in_db

    normalize_all_product_categories_in_db(dry_run=False)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tea_supply", "0038_customer_is_blocked"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
