# Generated manually: 履约状态、收货信息、商品可售库存

from django.db import migrations, models


def seed_stock(apps, schema_editor):
    Product = apps.get_model("tea_supply", "Product")
    Product.objects.filter(stock_quantity__lte=0).update(stock_quantity=10000.0)


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0015_reactivate_shop_catalog"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="stock_quantity",
            field=models.FloatField(
                default=0,
                help_text="未关联原材料时按此库存扣减；关联原材料则以原材料库存为准。",
                verbose_name="可售库存",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="workflow_status",
            field=models.CharField(
                choices=[
                    ("pending_confirm", "待确认"),
                    ("preparing", "备货中"),
                    ("shipped", "已发货"),
                    ("completed", "已完成"),
                ],
                default="pending_confirm",
                max_length=24,
                verbose_name="履约状态",
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="contact_name",
            field=models.CharField(blank=True, default="", max_length=100, verbose_name="收货人"),
        ),
        migrations.AddField(
            model_name="order",
            name="delivery_phone",
            field=models.CharField(blank=True, default="", max_length=30, verbose_name="联系电话"),
        ),
        migrations.AddField(
            model_name="order",
            name="store_name",
            field=models.CharField(blank=True, default="", max_length=200, verbose_name="门店/公司"),
        ),
        migrations.AddField(
            model_name="order",
            name="delivery_address",
            field=models.CharField(blank=True, default="", max_length=500, verbose_name="配送地址"),
        ),
        migrations.AddField(
            model_name="order",
            name="order_note",
            field=models.TextField(blank=True, default="", verbose_name="订单备注"),
        ),
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[("pending", "待处理"), ("paid", "已结算")],
                default="pending",
                max_length=20,
                verbose_name="结算状态",
            ),
        ),
        migrations.RunPython(seed_stock, migrations.RunPython.noop),
    ]
