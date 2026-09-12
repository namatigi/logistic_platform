from django.db import migrations, models


def backfill_escrow_amount(apps, schema_editor):
    EscrowAccount = apps.get_model('tenders', 'EscrowAccount')
    Invoice = apps.get_model('tenders', 'Invoice')
    for escrow in EscrowAccount.objects.all().only('id', 'amount').iterator():
        total = (
            Invoice.objects.filter(order__tender=escrow.tender)
            .aggregate(total=models.Sum('amount_total'))['total']
        )
        if total is not None and escrow.amount != total:
            escrow.amount = total
            escrow.save(update_fields=['amount'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('tenders', '0038_alter_order_order_id_order_uniq_order_per_company'),
    ]

    operations = [
        migrations.RunPython(backfill_escrow_amount, noop),
    ]