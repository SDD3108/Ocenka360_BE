from django.db import migrations


def seed_calendar(apps, schema_editor):
    apps.get_model("scheduling", "CalendarLock").objects.using(schema_editor.connection.alias).get_or_create(
        pk=1
    )


class Migration(migrations.Migration):
    dependencies = [("scheduling", "0001_initial")]
    operations = [migrations.RunPython(seed_calendar, migrations.RunPython.noop)]
