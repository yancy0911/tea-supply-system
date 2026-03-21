from django.db import migrations, models


def forwards_order_status(apps, schema_editor):
    Order = apps.get_model("tea_supply", "Order")
    Order.objects.filter(status__in=["已完成", "已取消"]).update(status="paid")
    Order.objects.filter(status__in=["待处理", "已确认", "已配货", "已发货"]).update(status="pending")
    Order.objects.exclude(status__in=["pending", "paid"]).update(status="pending")


class Migration(migrations.Migration):

    dependencies = [
        ("tea_supply", "0006_customer_credit_fields"),
    ]

    operations = [
        migrations.RunPython(forwards_order_status, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[("pending", "待处理"), ("paid", "已结算")],
                default="pending",
                max_length=20,
                verbose_name="订单状态",
            ),
        ),
    ]
