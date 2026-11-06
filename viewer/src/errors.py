"""Safe externally observable errors without exposing uploaded diagnostics."""


class ViewerError(Exception):
    """Carry a safe public error code, message and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        """Store the public error code, safe message and HTTP status."""
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class StorageError(ViewerError):
    """Report temporary storage failures without exposing provider diagnostics."""

    def __init__(self) -> None:
        """Report unavailable storage with a retryable HTTP 503 response."""
        super().__init__('storage_unavailable', 'Storage is unavailable; retry the operation.', 503)
