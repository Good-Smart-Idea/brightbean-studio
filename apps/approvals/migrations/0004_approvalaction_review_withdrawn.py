from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("approvals", "0003_alter_approvalaction_action")]

    operations = [
        migrations.AlterField(
            model_name="approvalaction",
            name="action",
            field=models.CharField(
                max_length=20,
                choices=[
                    ("submitted", "Submitted"),
                    ("approved", "Approved"),
                    ("changes_requested", "Changes Requested"),
                    ("rejected", "Rejected"),
                    ("resubmitted", "Resubmitted"),
                    ("held", "Hold Requested"),
                    ("withdrawn", "Review Withdrawn"),
                ],
            ),
        ),
    ]
