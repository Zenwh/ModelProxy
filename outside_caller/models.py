"""
模型白名单 + 对外名 ↔ MP 实际名 映射 + MP 端点路由。

MP 三种端点：
  - "messages"  → /v1/messages   (Anthropic claude_native 格式)
  - "responses" → /v1/responses  (OpenAI Responses API 格式)
  - "chat"      → /v1/chat/completions  (OpenAI Chat 格式)

由 bot 根据 endpoint 字段决定如何调 MP。
"""
from __future__ import annotations

from typing import List, Optional

# 对外暴露的 7 个模型
MODELS = {
    "claude-opus-4-7":    {"mp_name": "claude-opus-4-7",   "endpoint": "messages",  "vendor": "anthropic"},
    "claude-opus-4-6":    {"mp_name": "claude-opus-4-6",   "endpoint": "messages",  "vendor": "anthropic"},
    "claude-sonnet-4-6":  {"mp_name": "claude-sonnet-4-6", "endpoint": "messages",  "vendor": "anthropic"},
    "gpt-5-5":            {"mp_name": "gpt-5.5",           "endpoint": "responses", "vendor": "openai"},
    "gpt-5-4":            {"mp_name": "gpt-5.4",           "endpoint": "responses", "vendor": "openai"},
    "kimi-2.6":           {"mp_name": "kimi-k2.6",         "endpoint": "chat",      "vendor": "moonshot"},
    "glm-5.1":            {"mp_name": "glm-5.1",           "endpoint": "chat",      "vendor": "zhipu"},
}


def is_supported(pub_name: str) -> bool:
    """对外名是否在白名单。"""
    return pub_name in MODELS


def to_mp_name(pub_name: str) -> Optional[str]:
    """对外名 → MP 实际名。"""
    info = MODELS.get(pub_name)
    return info["mp_name"] if info else None


def to_endpoint(pub_name: str) -> Optional[str]:
    """对外名 → MP 端点类型 (messages / responses / chat)。"""
    info = MODELS.get(pub_name)
    return info["endpoint"] if info else None


def list_models() -> List[str]:
    """列出所有对外名。"""
    return list(MODELS.keys())


def get_info(pub_name: str) -> Optional[dict]:
    """获取模型详细信息。"""
    return MODELS.get(pub_name)
