# Generated manually for wholesale shop rules

from django.db import migrations, models


def sync_price_flags(apps, schema_editor):
    Product = apps.get_model("tea_supply", "Product")
    for p in Product.objects.all():
        ps = float(p.price_single or 0)
        pc = float(p.price_case or 0)
        p.price_on_request = (ps <= 0) or (pc <= 0)
        ul = (p.unit_label or "").strip()
        if not ul:
            p.unit_label = "per unit"
        cl = (p.case_label or "").strip()
        if not cl:
            p.case_label = "per case"
        p.save(
            update_fields=["price_on_request", "unit_label", "case_label"]
        )


class Migration(migrations.Migration):

    dependencies = [
        ("tea_supply", "0019_customer_account_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="catalog_upload",
            field=models.ImageField(
                blank=True,
                help_text="若上传则优先于上方「相对路径」在商城展示；可与 CSV 路径并存。",
                null=True,
                upload_to="products/uploaded/",
                verbose_name="上传主图",
            ),
        ),
        migrations.AddField(
            model_name="product",
            name="price_on_request",
            field=models.BooleanField(
                default=False,
                help_text="单品价或整箱价任一侧 ≤0 时自动为 True；商城仅展示「联系下单」，不可加入购物车。",
                verbose_name="询价商品",
            ),
        ),
        migrations.RunPython(sync_price_flags, migrations.RunPython.noop),
    ]
