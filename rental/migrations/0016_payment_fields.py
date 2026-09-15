from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0015_profile_is_cashier'),
    ]

    operations = [
        migrations.AddField(
            model_name='suitrequest',
            name='is_paid',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='suitrequest',
            name='payment_method',
            field=models.CharField(blank=True, max_length=50, null=True),
        ),
        migrations.AddField(
            model_name='suitrequest',
            name='payment_reference',
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
    ]
