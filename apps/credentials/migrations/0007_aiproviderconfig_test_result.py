from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("credentials", "0006_aiproviderconfig"),
    ]

    operations = [
        migrations.AddField(
            model_name="aiproviderconfig",
            name="test_message",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="aiproviderconfig",
            name="test_result",
            field=models.CharField(
                choices=[("success", "Success"), ("failure", "Failure"), ("untested", "Untested")],
                default="untested",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="aiproviderconfig",
            name="tested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
