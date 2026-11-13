from django.db import migrations, models
from django.db.models import F


def record_percent_discounts(apps, schema_editor):
    """Every sale so far was discounted by a percentage: note the figure that was typed."""
    Sale = apps.get_model("pos", "Sale")
    Sale.objects.exclude(discount_rate=0).update(discount_value=F("discount_rate"))


class Migration(migrations.Migration):

    dependencies = [
        ('pos', '0044_bank_receipts'),
    ]

    operations = [
        migrations.AlterField(
            model_name="sale",
            name="discount_rate",
            field=models.DecimalField(decimal_places=10, default=0, max_digits=13),
        ),
        migrations.AddField(
            model_name="sale",
            name="discount_type",
            field=models.CharField(
                choices=[("percent", "Percentage"), ("amount", "Cash amount")], default="percent", max_length=10
            ),
        ),
        migrations.AddField(
            model_name="sale",
            name="discount_value",
            field=models.DecimalField(decimal_places=2, default=0, max_digits=12),
        ),
        migrations.RunPython(record_percent_discounts, migrations.RunPython.noop),
    ]
