from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("tea_supply", "0051_product_official_image_url"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="tier_prices",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text='最小可用阶梯价模板，如 {"1": 12, "10": 10, "50": 8}。',
                verbose_name="阶梯价模板",
            ),
        ),
    ]
