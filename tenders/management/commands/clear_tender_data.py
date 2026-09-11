from django.core.management.base import BaseCommand

from tenders.models import EscrowAccount, Invoice, Order, OrderLine, PendingPush, Tender, TenderSubmission


class Command(BaseCommand):
    help = (
        'Permanently delete all tenders, orders, invoices and escrow accounts '
        'from the configured database. Related rows (order lines, tender '
        'submissions, pending pushes, escrow accounts) are removed by cascade. '
        'Runs against whatever database the environment points at (DATABASE_URL).'
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--confirm',
            action='store_true',
            help='Confirm the deletion. Without this flag the command only reports counts.',
        )

    def handle(self, *args, **options):
        tables = (
            ('Tenders', Tender),
            ('Orders', Order),
            ('Order lines', OrderLine),
            ('Invoices', Invoice),
            ('Escrow accounts', EscrowAccount),
            ('Tender submissions', TenderSubmission),
            ('Pending pushes', PendingPush),
        )
        before = {label: model.objects.count() for label, model in tables}

        if not options['confirm']:
            self.stdout.write(self.style.WARNING('Dry run — nothing deleted.'))
            for label, count in before.items():
                self.stdout.write(f'  {label}: {count}')
            self.stdout.write(self.style.WARNING('Re-run with --confirm to delete these records.'))
            return

        Invoice.objects.all().delete()
        Order.objects.all().delete()
        Tender.objects.all().delete()

        self.stdout.write(self.style.SUCCESS('Deleted.'))
        for label, model in tables:
            self.stdout.write(f'  {label}: {before[label]} deleted, {model.objects.count()} remaining')