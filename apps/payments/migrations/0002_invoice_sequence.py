from django.db import migrations


def seed(apps, schema_editor):
    apps.get_model("payments", "InvoiceSequence").objects.using(schema_editor.connection.alias).get_or_create(
        pk=1
    )


class Migration(migrations.Migration):
    dependencies = [("payments", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
