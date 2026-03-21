from django.db import migrations, models


def forwards_map_levels(apps, schema_editor):
    Customer = apps.get_model("tea_supply", "Customer")
    for c in Customer.objects.all():
        lv = (c.customer_level or "").strip()
        if lv in ("VIP", "VIP客户"):
            c.customer_level = "VIP"
        elif lv == "A":
            c.customer_level = "A"
        elif lv == "B":
            c.customer_level = "B"
        else:
            c.customer_level = "C"
        c.save(update_fields=["customer_level"])


def forwards_recompute_tiers(apps, schema_editor):
    Customer = apps.get_model("tea_supply", "Customer")
    OrderItem = apps.get_model("tea_supply", "OrderItem")
    Ingredient = apps.get_model("tea_supply", "Ingredient")
    for cust in Customer.objects.all():
        total = 0.0
        for oi in OrderItem.objects.filter(order__customer_id=cust.pk):
            ing = Ingredient.objects.get(pk=oi.ingredient_id)
            total += float(oi.quantity) * float(ing.price)
        if total < 200:
            level, allow, credit = "C", False, 0.0
        elif total < 500:
            level, allow, credit = "B", True, 100.0
        elif total < 1000:
            level, allow, credit = "A", True, 300.0
        else:
            level, allow, credit = "VIP", True, 1000.0
        Customer.objects.filter(pk=cust.pk).update(
            customer_level=level,
            allow_credit=allow,
            credit_limit=credit,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("tea_supply", "0007_order_status_pending_paid"),
    ]

    operations = [
        migrations.RunPython(forwards_map_levels, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="customer",
            name="customer_level",
            field=models.CharField(
                choices=[("C", "C"), ("B", "B"), ("A", "A"), ("VIP", "VIP")],
                default="C",
                max_length=10,
                verbose_name="客户等级",
            ),
        ),
        migrations.RunPython(forwards_recompute_tiers, migrations.RunPython.noop),
    ]
