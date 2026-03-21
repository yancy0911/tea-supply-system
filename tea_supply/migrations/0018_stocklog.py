# 库存流水 StockLog

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0017_order_stock_deducted"),
    ]

    operations = [
        migrations.CreateModel(
            name="StockLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("direction", models.CharField(choices=[("OUT", "出库"), ("IN", "入库")], max_length=3, verbose_name="方向")),
                ("quantity", models.FloatField(verbose_name="数量")),
                ("remark", models.CharField(blank=True, default="", max_length=240, verbose_name="备注")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="时间")),
                (
                    "ingredient",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stock_logs",
                        to="tea_supply.ingredient",
                        verbose_name="原材料",
                    ),
                ),
                (
                    "order",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="stock_logs",
                        to="tea_supply.order",
                        verbose_name="关联订单",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="stock_logs",
                        to="tea_supply.product",
                        verbose_name="商品",
                    ),
                ),
            ],
            options={
                "verbose_name": "库存流水",
                "verbose_name_plural": "库存流水",
                "ordering": ("-created_at",),
            },
        ),
    ]
