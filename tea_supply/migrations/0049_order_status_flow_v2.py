# Order lifecycle V2: unified status field; remove workflow_status + legacy settlement column.

from django.db import migrations, models


def forwards_fill(apps, schema_editor):
    Order = apps.get_model("tea_supply", "Order")
    for o in Order.objects.all():
        ws = o.workflow_status
        ls = o.legacy_settlement_status
        if ws == "cancelled":
            lifecycle = "cancelled"
        elif ws == "completed":
            lifecycle = "completed"
        elif ws == "shipped":
            lifecycle = "shipping"
        elif ws == "preparing":
            lifecycle = "picking"
        elif ws == "confirmed":
            lifecycle = "paid" if ls == "paid" else "confirmed"
        elif ws == "pending_confirm":
            lifecycle = "pending"
        else:
            lifecycle = "pending"
        o.lifecycle_status = lifecycle
        o.save(update_fields=["lifecycle_status"])


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0048_userrole_rbac_v1"),
    ]

    operations = [
        migrations.RenameField(
            model_name="order",
            old_name="status",
            new_name="legacy_settlement_status",
        ),
        migrations.AddField(
            model_name="order",
            name="lifecycle_status",
            field=models.CharField(default="pending", max_length=24),
        ),
        migrations.RunPython(forwards_fill, backwards_noop),
        migrations.RemoveField(model_name="order", name="workflow_status"),
        migrations.RemoveField(model_name="order", name="legacy_settlement_status"),
        migrations.RenameField(
            model_name="order",
            old_name="lifecycle_status",
            new_name="status",
        ),
        migrations.AlterField(
            model_name="order",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "待确认"),
                    ("confirmed", "已确认"),
                    ("paid", "已付款"),
                    ("picking", "备货中"),
                    ("shipping", "配送中"),
                    ("completed", "完成"),
                    ("cancelled", "已取消"),
                ],
                default="pending",
                max_length=24,
                verbose_name="订单状态",
            ),
        ),
    ]
