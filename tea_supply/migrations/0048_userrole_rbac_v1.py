# UserRole RBAC V1: staff -> manager, add missing roles, choices update.

from django.db import migrations, models


def forwards_staff_to_manager(apps, schema_editor):
    UserRole = apps.get_model("tea_supply", "UserRole")
    UserRole.objects.filter(role="staff").update(role="manager")


def forwards_missing_roles(apps, schema_editor):
    User = apps.get_model("auth", "User")
    UserRole = apps.get_model("tea_supply", "UserRole")
    have = set(UserRole.objects.values_list("user_id", flat=True))
    for u in User.objects.all().iterator():
        if u.pk in have:
            continue
        if getattr(u, "is_superuser", False):
            role = "owner"
        elif getattr(u, "is_staff", False):
            role = "manager"
        else:
            role = "customer"
        UserRole.objects.create(user_id=u.pk, role=role)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("tea_supply", "0047_fix_customer_level_columns"),
    ]

    operations = [
        migrations.RunPython(forwards_staff_to_manager, noop),
        migrations.RunPython(forwards_missing_roles, noop),
        migrations.AlterField(
            model_name="userrole",
            name="role",
            field=models.CharField(
                choices=[
                    ("owner", "老板"),
                    ("manager", "经理"),
                    ("warehouse", "仓库"),
                    ("driver", "司机"),
                    ("customer", "客户"),
                ],
                default="customer",
                max_length=16,
                verbose_name="角色",
            ),
        ),
    ]
