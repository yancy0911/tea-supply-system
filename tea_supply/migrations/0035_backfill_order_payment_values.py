from django.db import migrations


def forward(apps, schema_editor):
    Order = apps.get_model("tea_supply", "Order")
    Order.objects.filter(payment_status="pending_transfer").update(payment_status="pending_confirmation")
    Order.objects.filter(payment_status="failed").update(payment_status="cancelled")
    Order.objects.filter(payment_method="stripe").update(payment_method="card_on_pickup")


def backward(apps, schema_editor):
    Order = apps.get_model("tea_supply", "Order")
    Order.objects.filter(payment_status="pending_confirmation").update(payment_status="pending_transfer")
    Order.objects.filter(payment_status="cancelled").update(payment_status="failed")
    Order.objects.filter(payment_method="card_on_pickup").update(payment_method="stripe")


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0034_alter_order_payment_method_and_more"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]

