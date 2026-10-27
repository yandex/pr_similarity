"""Safe externally observable errors without exposing uploaded diagnostics."""


class ViewerError(Exception):
    """Carry a safe public error code, message and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 422) -> None:
        """Initialize the resources and limits owned by this instance."""
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class StorageError(ViewerError):
    """Report temporary storage failures without exposing provider diagnostics."""

    def __init__(self) -> None:
        """Initialize the resources and limits owned by this instance."""
        super().__init__('storage_unavailable', 'Storage is unavailable; retry the operation.', 503)
