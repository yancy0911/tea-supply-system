# 备货时扣库存；取消/退回时恢复

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0016_order_workflow_product_stock"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="stock_deducted",
            field=models.BooleanField(
                default=False,
                help_text="进入「备货中」时扣减；取消或退回待确认时恢复。勿随意手改。",
                verbose_name="已扣库存",
            ),
        ),
        migrations.AlterField(
            model_name="order",
            name="workflow_status",
            field=models.CharField(
                choices=[
                    ("pending_confirm", "待确认"),
                    ("preparing", "备货中"),
                    ("shipped", "已发货"),
                    ("completed", "已完成"),
                    ("cancelled", "已取消"),
                ],
                default="pending_confirm",
                max_length=24,
                verbose_name="履约状态",
            ),
        ),
    ]
