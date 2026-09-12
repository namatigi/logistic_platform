from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0035_odoo_truck_alias'),
    ]

    operations = [
        migrations.RenameField(
            model_name='tender',
            old_name='cargo_reference',
            new_name='trans_reference',
        ),
        migrations.RenameField(
            model_name='tendersubmission',
            old_name='cargo_reference',
            new_name='trans_reference',
        ),
        migrations.RenameField(
            model_name='order',
            old_name='cargo_reference',
            new_name='trans_reference',
        ),
    ]