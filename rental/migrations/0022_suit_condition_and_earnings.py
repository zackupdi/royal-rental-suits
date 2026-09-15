# Generated migration for new Suit fields

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('rental', '0021_profile_is_reception'),
    ]

    operations = [
        migrations.AddField(
            model_name='suit',
            name='condition_grade',
            field=models.CharField(
                choices=[('new', 'Cusub (New)'), ('medium', 'Dhexe (Medium)'), ('retired', 'Aad u Raagay (Retired/In-active)')],
                default='new',
                max_length=20
            ),
        ),
        migrations.AddField(
            model_name='suit',
            name='damage_notes',
            field=models.TextField(blank=True, help_text='Halkan ku qor haddii uu leeyahay jeexitaan, buraash, iwd.', null=True),
        ),
        migrations.AddField(
            model_name='suit',
            name='total_earnings',
            field=models.DecimalField(decimal_places=2, default=0.0, max_digits=10),
        ),
        migrations.AddField(
            model_name='suit',
            name='rental_count',
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name='suit',
            name='rented_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='suit',
            name='created_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='suit',
            name='updated_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='suit',
            name='status',
            field=models.CharField(
                choices=[('Available', 'Available'), ('Rented', 'Rented'), ('Cleaning', 'Cleaning Service')],
                default='Available',
                max_length=20
            ),
        ),
    ]
