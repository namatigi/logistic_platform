from django.core.management.base import BaseCommand

from tenders.views import flush_pending_pushes


class Command(BaseCommand):
    help = (
        'Re-send queued tender submissions (PendingPush) whose first push failed, '
        'and mark them delivered on success. Safe to run on a schedule.'
    )

    def handle(self, *args, **options):
        delivered, failed, skipped = flush_pending_pushes()
        self.stdout.write(
            self.style.SUCCESS(
                f'flush_pending_pushes: {delivered} delivered, '
                f'{failed} permanently failed, {skipped} skipped (no base URL configured).'
            )
        )