# 恢复商城可见：批量启用商品与分类（避免 is_active=False 导致 /shop/ 列表为空）

from django.db import migrations


def forwards(apps, schema_editor):
    Product = apps.get_model("tea_supply", "Product")
    ProductCategory = apps.get_model("tea_supply", "ProductCategory")
    Product.objects.all().update(is_active=True)
    ProductCategory.objects.all().update(is_active=True)


def backwards(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0014_product_image"),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
