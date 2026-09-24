"""AppError: HTTP-visible failures raised by handlers and node ops."""
from __future__ import annotations


class AppError(Exception):
    """A failure the HTTP surface answers with its own status and text."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status
