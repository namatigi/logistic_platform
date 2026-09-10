import logging

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from storages.backends.s3 import S3Storage

logger = logging.getLogger(__name__)


class MediaStorage:
    """Delegating media storage selected by the administrator.

    Option A (local): files live on a persistent server volume (e.g. a Railway
    Volume mounted at MEDIA_ROOT) so uploads survive redeploys.

    Option B (s3): files live in S3-compatible object storage (AWS S3, Cloudflare
    R2, DigitalOcean Spaces, ...).

    The active option is chosen on Configuration > Files, so the delegation is
    resolved per file operation rather than at import time.
    """

    def __init__(self, **options):
        self._file_system = FileSystemStorage(
            location=settings.MEDIA_ROOT,
            base_url=settings.MEDIA_URL,
        )
        self._s3 = None

    @property
    def backend(self):
        if self._is_s3_enabled():
            if self._s3 is None:
                self._s3 = S3Storage()
            return self._s3
        return self._file_system

    def _is_s3_enabled(self):
        from tenders.models import ApiSetting

        option = ApiSetting.get().media_storage
        if option != ApiSetting.MediaStorageOption.S3:
            return False
        if not getattr(settings, 'AWS_STORAGE_BUCKET_NAME', '').strip():
            logger.warning(
                'Media storage is set to S3 but AWS_STORAGE_BUCKET_NAME is not '
                'configured; falling back to the server volume.'
            )
            return False
        return True

    def __getattr__(self, name):
        return getattr(self.backend, name)