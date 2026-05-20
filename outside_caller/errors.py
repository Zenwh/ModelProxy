"""
OpenAI-compatible error format.

把 HTTPException 转成 OpenAI 标准的:
{
  "error": {
    "message": "...",
    "type":    "invalid_request_error|authentication_error|rate_limit_error|api_error|server_error",
    "code":    "..." | null,
    "param":   "..." | null
  }
}
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


# 标准类型映射（OpenAI 的）
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",
    502: "api_error",
    503: "api_error",
    504: "api_error",
}


class OpenAIError(HTTPException):
    """带 OpenAI 错误类型 / code / retry-after 的 HTTPException。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        status: int = 400,
        code: Optional[str] = None,
        param: Optional[str] = None,
        retry_after: Optional[int] = None,
    ):
        self.error_type = error_type
        self.error_code = code
        self.error_param = param
        self.retry_after = retry_after
        super().__init__(status_code=status, detail=message)


def _infer_type(status: int) -> str:
    return ERROR_TYPES.get(status, "api_error")


async def openai_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """全局 exception handler：包装成 OpenAI 格式。"""
    error_type = getattr(exc, "error_type", _infer_type(exc.status_code))
    error_code = getattr(exc, "error_code", None)
    error_param = getattr(exc, "error_param", None)
    retry_after = getattr(exc, "retry_after", None)

    body = {
        "error": {
            "message": exc.detail,
            "type": error_type,
            "code": error_code,
            "param": error_param,
        }
    }
    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)
