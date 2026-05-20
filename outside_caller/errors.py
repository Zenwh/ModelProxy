"""
OpenAI-compatible error format + Anthropic-compatible error format.

OpenAI 格式：
{
  "error": {
    "message": "...",
    "type":    "invalid_request_error|authentication_error|...",
    "code":    "..." | null,
    "param":   "..." | null
  }
}

Anthropic 格式：
{
  "type": "error",
  "error": {
    "type": "invalid_request_error|authentication_error|...",
    "message": "..."
  }
}

handler 根据请求 path 决定包装哪种格式：
  - /v1/messages → Anthropic 格式
  - 其他          → OpenAI 格式
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


# 状态码 → 错误类型（OpenAI/Anthropic 名字一致）
ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "server_error",          # Anthropic 用 api_error
    502: "api_error",
    503: "api_error",
    504: "api_error",
}

# Anthropic 用 api_error 替代 server_error
ANTHROPIC_ERROR_REMAP = {
    "server_error": "api_error",
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


class AnthropicError(HTTPException):
    """专给 /v1/messages 路径用的错误类型。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        status: int = 400,
        retry_after: Optional[int] = None,
    ):
        self.error_type = error_type
        self.retry_after = retry_after
        super().__init__(status_code=status, detail=message)


def _infer_type(status: int) -> str:
    return ERROR_TYPES.get(status, "api_error")


def _is_anthropic_path(request: Request) -> bool:
    """根据请求 path 判断是否走 Anthropic 错误格式。"""
    p = request.url.path
    return p.endswith("/v1/messages") or "/v1/messages/" in p


def _to_anthropic_body(error_type: str, message: str) -> dict:
    error_type = ANTHROPIC_ERROR_REMAP.get(error_type, error_type)
    return {
        "type": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }


def _to_openai_body(error_type: str, message: str, code, param) -> dict:
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": code,
            "param": param,
        }
    }


async def error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """全局 exception handler：根据请求路径选 OpenAI 或 Anthropic 格式。"""
    error_type = getattr(exc, "error_type", _infer_type(exc.status_code))
    error_code = getattr(exc, "error_code", None)
    error_param = getattr(exc, "error_param", None)
    retry_after = getattr(exc, "retry_after", None)

    headers = {}
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)

    # 显式 AnthropicError 或者请求走 /v1/messages 路径 → Anthropic 格式
    if isinstance(exc, AnthropicError) or _is_anthropic_path(request):
        body = _to_anthropic_body(error_type, exc.detail)
    else:
        body = _to_openai_body(error_type, exc.detail, error_code, error_param)

    return JSONResponse(status_code=exc.status_code, content=body, headers=headers)


# 向后兼容旧名字
openai_error_handler = error_handler
