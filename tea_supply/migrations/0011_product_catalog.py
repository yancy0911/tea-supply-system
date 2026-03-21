# Generated manually for Product catalog + OrderItem migration

import django.db.models.deletion
from django.db import migrations, models


def migrate_products_and_lines(apps, schema_editor):
    ProductCategory = apps.get_model("tea_supply", "ProductCategory")
    Product = apps.get_model("tea_supply", "Product")
    Ingredient = apps.get_model("tea_supply", "Ingredient")
    OrderItem = apps.get_model("tea_supply", "OrderItem")
    Order = apps.get_model("tea_supply", "Order")

    cat, _ = ProductCategory.objects.get_or_create(
        name="默认分类",
        defaults={"sort_order": 0, "is_active": True},
    )
    ing_to_product = {}
    for ing in Ingredient.objects.all():
        sku = f"ING-{ing.pk}"
        p, _ = Product.objects.get_or_create(
            sku=sku,
            defaults={
                "category_id": cat.pk,
                "name": ing.name,
                "unit_label": ing.unit,
                "case_label": "",
                "price_single": float(ing.price),
                "price_case": float(ing.price),
                "cost_price_single": float(ing.cost_price),
                "cost_price_case": float(ing.cost_price),
                "shelf_life_months": 12,
                "can_split_sale": True,
                "minimum_order_qty": 0.01,
                "is_active": True,
                "ingredient_id": ing.pk,
                "units_per_case": 1.0,
            },
        )
        ing_to_product[ing.pk] = p.pk

    for oi in OrderItem.objects.all():
        pid = ing_to_product.get(oi.ingredient_id)
        if not pid:
            continue
        p = Product.objects.get(pk=pid)
        q = float(oi.quantity)
        unit_price = float(p.price_single)
        tr = q * unit_price
        tc = q * float(p.cost_price_single)
        pr = tr - tc
        OrderItem.objects.filter(pk=oi.pk).update(
            product_id=pid,
            sale_type="single",
            unit_price=unit_price,
            total_revenue=tr,
            total_cost=tc,
            profit=pr,
        )

    for order in Order.objects.all():
        tr = 0.0
        tc = 0.0
        for item in OrderItem.objects.filter(order_id=order.pk):
            tr += float(item.total_revenue)
            tc += float(item.total_cost)
        Order.objects.filter(pk=order.pk).update(
            total_revenue=tr,
            total_cost=tc,
            profit=tr - tc,
        )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("tea_supply", "0010_inventory_and_profit"),
    ]

    operations = [
        migrations.CreateModel(
            name="ProductCategory",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=100, verbose_name="分类名称")),
                ("sort_order", models.IntegerField(default=0, verbose_name="排序")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
            ],
            options={
                "ordering": ("sort_order", "id"),
            },
        ),
        migrations.CreateModel(
            name="Product",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(max_length=200, verbose_name="商品名称")),
                ("sku", models.CharField(max_length=64, unique=True, verbose_name="SKU")),
                ("unit_label", models.CharField(blank=True, default="", max_length=120, verbose_name="单位规格")),
                ("case_label", models.CharField(blank=True, default="", max_length=120, verbose_name="整箱规格")),
                ("price_single", models.FloatField(default=0, verbose_name="单品价")),
                ("price_case", models.FloatField(default=0, verbose_name="整箱价")),
                ("cost_price_single", models.FloatField(default=0, verbose_name="单品成本")),
                ("cost_price_case", models.FloatField(default=0, verbose_name="整箱成本")),
                ("shelf_life_months", models.PositiveSmallIntegerField(default=12, verbose_name="保质期（月）")),
                ("can_split_sale", models.BooleanField(default=True, verbose_name="是否可拆卖")),
                ("minimum_order_qty", models.FloatField(default=0.01, verbose_name="起订量")),
                ("is_active", models.BooleanField(default=True, verbose_name="是否启用")),
                ("units_per_case", models.FloatField(default=1, help_text="整箱下单时：扣减库存 = 数量 × 本字段；单品下单时：扣减数量 = 下单数量。", verbose_name="整箱对应库存扣减数量")),
                (
                    "category",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="products",
                        to="tea_supply.productcategory",
                        verbose_name="分类",
                    ),
                ),
                (
                    "ingredient",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to="tea_supply.ingredient",
                        verbose_name="关联原材料（库存扣减）",
                    ),
                ),
            ],
            options={
                "ordering": ("category", "name"),
            },
        ),
        migrations.AddField(
            model_name="orderitem",
            name="product",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="tea_supply.product",
                verbose_name="商品",
            ),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="profit",
            field=models.FloatField(default=0, verbose_name="行利润"),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="sale_type",
            field=models.CharField(
                choices=[("single", "单品"), ("case", "整箱")],
                default="single",
                max_length=10,
                verbose_name="销售方式",
            ),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="total_cost",
            field=models.FloatField(default=0, verbose_name="行成本"),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="total_revenue",
            field=models.FloatField(default=0, verbose_name="行收入"),
        ),
        migrations.AddField(
            model_name="orderitem",
            name="unit_price",
            field=models.FloatField(default=0, verbose_name="单价"),
        ),
        migrations.RunPython(migrate_products_and_lines, noop_reverse),
        migrations.RemoveField(
            model_name="orderitem",
            name="ingredient",
        ),
        migrations.AlterField(
            model_name="orderitem",
            name="product",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                to="tea_supply.product",
                verbose_name="商品",
            ),
        ),
    ]
