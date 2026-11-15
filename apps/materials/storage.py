from django.conf import settings
from django.core.files.storage import FileSystemStorage


class PrivateStorage(FileSystemStorage):
    def __init__(self):
        super().__init__(
            location=settings.MATERIAL_STORAGE_ROOT,
            file_permissions_mode=0o600,
            directory_permissions_mode=0o700,
        )

    def url(self, name):
        raise NotImplementedError("Private files have no public URL.")
