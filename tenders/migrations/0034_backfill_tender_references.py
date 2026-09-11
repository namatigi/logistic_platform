from django.db import migrations


def backfill_references(apps, schema_editor):
    """Assign every tender that predates the `reference` field its unique number.

    Mirrors Tender.make_reference() so pre-existing tenders display the same
    HX-YYMM-<id> format and group orders like newly created ones.
    """
    Tender = apps.get_model('tenders', 'Tender')
    batch = []
    for tender in Tender.objects.all().iterator():
        if tender.reference:
            continue
        stamp = tender.created_at
        tender.reference = f'HX-{stamp.year % 100:02d}{stamp.month:02d}-{tender.pk:06d}'
        batch.append(tender)
        if len(batch) >= 500:
            Tender.objects.bulk_update(batch, ['reference'])
            batch = []
    if batch:
        Tender.objects.bulk_update(batch, ['reference'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0033_tender_reference'),
    ]

    operations = [
        migrations.RunPython(backfill_references, noop),
    ]