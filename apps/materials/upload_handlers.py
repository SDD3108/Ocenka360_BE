from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler
from rest_framework.exceptions import APIException


class UploadTooLarge(APIException):
    status_code = 413
    default_code = "upload_too_large"
    default_detail = "Upload exceeds the allowed size or file count."


class BoundedUploadHandler(FileUploadHandler):
    """Runs before memory/temp handlers, including when CSRF parses a multipart body."""

    def __init__(self, request=None):
        super().__init__(request)
        self.received = 0
        self.files = 0

    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.files += 1
        if self.files > 1:
            raise UploadTooLarge()

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > settings.MATERIAL_MAX_FILE_BYTES:
            raise UploadTooLarge()
        return raw_data

    def file_complete(self, file_size):
        return None
