"""Unified business exceptions and standard error codes for Personal OKF-Wiki."""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    CONFIG_INVALID = "CONFIG_INVALID"
    WRITE_LOCK_TIMEOUT = "WRITE_LOCK_TIMEOUT"
    WATCHER_ALREADY_RUNNING = "WATCHER_ALREADY_RUNNING"
    ACTIVE_SESSION_EXISTS = "ACTIVE_SESSION_EXISTS"
    SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
    GIT_OPERATION_FAILED = "GIT_OPERATION_FAILED"
    OKF_SCHEMA_ERROR = "OKF_SCHEMA_ERROR"
    BROKEN_LINK = "BROKEN_LINK"
    HUMAN_BLOCK_MODIFIED = "HUMAN_BLOCK_MODIFIED"
    VERIFIED_CONTENT_MODIFIED = "VERIFIED_CONTENT_MODIFIED"
    UNEXPECTED_FILE_DELETED = "UNEXPECTED_FILE_DELETED"
    CONTENT_SHRINKAGE = "CONTENT_SHRINKAGE"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    PATH_CONFLICT = "PATH_CONFLICT"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    ADAPTER_ERROR = "ADAPTER_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class KBError(Exception):
    """Structured domain error serialized cleanly in CLI --json responses."""

    def __init__(
        self,
        error_code: ErrorCode | str,
        message: str,
        details: dict[str, Any] | None = None,
        exit_code: int = 1,
    ) -> None:
        super().__init__(message)
        self.error_code = (
            error_code.value if isinstance(error_code, ErrorCode) else str(error_code)
        )
        self.message = message
        self.details = details or {}
        self.exit_code = exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": self.error_code,
            "message": self.message,
            "details": self.details,
        }
