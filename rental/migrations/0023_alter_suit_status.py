# Generated migration for expanded suit status workflow

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0022_suit_condition_and_earnings'),
    ]

    operations = [
        migrations.AlterField(
            model_name='suit',
            name='status',
            field=models.CharField(
                choices=[
                    ('Available', 'Available'),
                    ('Pending Request', 'Pending Request'),
                    ('Reserved', 'Reserved'),
                    ('Rented', 'Rented'),
                    ('Returned', 'Returned'),
                    ('Dry Cleaning', 'Dry Cleaning'),
                ],
                default='Available',
                max_length=20,
            ),
        ),
    ]
