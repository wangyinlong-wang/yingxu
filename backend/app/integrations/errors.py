"""Safe, user-presentable integration errors."""

from __future__ import annotations


class IntegrationError(Exception):
    """An expected integration failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, *, uncertain: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain

    def __str__(self) -> str:
        return self.message


class CanceledError(IntegrationError):
    """Raised when a caller-requested cancellation has stopped an operation."""

    def __init__(self, message: str = "操作已取消") -> None:
        super().__init__("CANCELED", message)
