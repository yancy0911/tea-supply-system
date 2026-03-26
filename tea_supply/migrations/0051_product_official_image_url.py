from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0050_add_stripe_payment_method"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="official_image_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="统一图片数据源：优先使用此 URL（由 mochaboba.com 同步/映射生成）。",
                max_length=500,
                verbose_name="官网高清图（统一）",
            ),
        ),
    ]

