from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("composer", "0023_platformpost_youtube_video_checked_at")]
    operations = [
        migrations.AddField(
            model_name="platformpost",
            name="native_upload_state",
            field=models.JSONField(blank=True, default=dict, db_default={}, editable=False),
        )
    ]
