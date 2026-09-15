from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("rental", "0018_suitrequest_customer_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="suitrequest",
            name="first_name",
            field=models.CharField(max_length=50, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="suitrequest",
            name="second_name",
            field=models.CharField(max_length=50, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="suitrequest",
            name="present_address",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="suitrequest",
            name="parents_address",
            field=models.CharField(max_length=255, null=True, blank=True),
        ),
        migrations.AddField(
            model_name="suitrequest",
            name="notes",
            field=models.TextField(null=True, blank=True),
        ),
    ]
