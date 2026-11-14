"""应用统一异常与错误码。"""
from __future__ import annotations


class AppError(Exception):
    """业务异常：携带错误码与用户可读信息。"""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


# 错误码常量（与文档第 6 节对齐）
ERR_URL_INVALID = "ERR_URL_INVALID"
ERR_VIDEO_UNAVAILABLE = "ERR_VIDEO_UNAVAILABLE"
ERR_DOWNLOAD_FAILED = "ERR_DOWNLOAD_FAILED"
ERR_ASR_FAILED = "ERR_ASR_FAILED"
ERR_ASR_TIMEOUT = "ERR_ASR_TIMEOUT"
ERR_LLM_FAILED = "ERR_LLM_FAILED"
ERR_TOO_LONG = "ERR_TOO_LONG"
ERR_CANCELLED = "ERR_CANCELLED"
ERR_NOT_FOUND = "ERR_NOT_FOUND"
ERR_NOT_READY = "ERR_NOT_READY"
ERR_INTERNAL = "ERR_INTERNAL"