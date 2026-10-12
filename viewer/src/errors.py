"""Safe externally observable errors without exposing uploaded diagnostics."""


class ViewerError(Exception):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class StorageError(ViewerError):
    def __init__(self):
        super().__init__('storage_unavailable', 'Storage is unavailable; retry the operation.', 503)
